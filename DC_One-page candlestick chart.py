import os
import sys
import re
import time
import requests
import pandas as pd
import numpy as np
import yfinance as yf
import mplfinance as mpf
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from io import StringIO
from datetime import datetime, timedelta

# Selenium 相關
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ================= 設定區 =================
STOCK_ID = "2455"  # 在此修改你要的股票代碼
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_TEST")

# 顏色定義 (台股：紅漲綠跌)
COLOR_UP = '#ef5350'   
COLOR_DOWN = '#26a69a' 

# 設定中文字型 (針對 GitHub Actions Ubuntu 環境優化)
import matplotlib.font_manager as fm
font_candidates = ['WenQuanYi Zen Hei', 'Microsoft JhengHei', 'SimHei', 'Arial Unicode MS']
font_path = None
plt.rcParams['font.sans-serif'] = ['sans-serif'] # Fallback
for f in font_candidates:
    # 檢查系統是否有該字型
    if any(f in font.name for font in fm.fontManager.ttflist):
        plt.rcParams['font.sans-serif'] = [f]
        plt.rcParams['axes.unicode_minus'] = False
        print(f"✅ 使用字型: {f}")
        break

# ================= 1. 工具函數 =================

def is_roc_date(s: str) -> bool:
    """檢查是否為民國日期格式 (e.g. 112/01/01)"""
    return re.match(r"\d{2,3}/\d{1,2}/\d{1,2}", str(s).strip()) is not None

def roc_to_datestr(d_str: str):
    """將民國日期轉為 YYYY-MM-DD"""
    try:
        parts = re.split(r"[/-]", str(d_str).strip())
        if len(parts) < 2: return None
        y = int(parts[0])
        y = y + 1911 if y < 1911 else y
        m = int(parts[1])
        d = int(parts[2]) if len(parts) > 2 else 1
        return f"{y:04d}-{m:02d}-{d:02d}"
    except:
        return None

def calculate_technical_indicators(df):
    df = df.copy()
    # MA
    df['MA5'] = df['Close'].rolling(5).mean()
    df['MA10'] = df['Close'].rolling(10).mean()
    df['MA20'] = df['Close'].rolling(20).mean()
    df['MA60'] = df['Close'].rolling(60).mean()
    # BB (布林通道)
    df['BB_Mid'] = df['Close'].rolling(20).mean()
    df['BB_Std'] = df['Close'].rolling(20).std()
    df['BB_Up'] = df['BB_Mid'] + 2 * df['BB_Std']
    df['BB_Low'] = df['BB_Mid'] - 2 * df['BB_Std']
    # 布林寬帶 (Bandwidth) % = (Up - Low) / Mid
    df['BB_Width'] = ((df['BB_Up'] - df['BB_Low']) / df['BB_Mid']) * 100
    return df

# ================= 2. 爬蟲功能 (Selenium) =================

def get_driver():
    """設定 Chrome Driver，適配 GitHub Actions 環境"""
    options = Options()
    options.add_argument('--headless=new') # 無頭模式
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    # 偽裝 User-Agent 防止被擋
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    # GitHub Actions Ubuntu 的 Chrome 路徑通常在這裡
    if os.path.exists("/usr/bin/chromium-browser"):
        options.binary_location = "/usr/bin/chromium-browser"
    elif os.path.exists("/usr/bin/google-chrome"):
        options.binary_location = "/usr/bin/google-chrome"

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)

def get_stock_price(stock_id):
    print(f"[{stock_id}] 1. 抓取股價 (Yahoo)...")
    try:
        df = yf.Ticker(f"{stock_id}.TW").history(period="1y")
        if df.empty: 
            df = yf.Ticker(f"{stock_id}.TWO").history(period="1y")
        
        if df.empty: return None
        
        df.index = df.index.tz_localize(None)
        df['DateStr'] = df.index.strftime('%Y-%m-%d')
        df = calculate_technical_indicators(df)
        return df
    except Exception as e:
        print(f"❌ 股價抓取失敗: {e}")
        return None

def get_chips_fubon(stock_id, start_date, end_date):
    """從富邦證券抓取 外資、投信、融資"""
    print(f"[{stock_id}] 2. 抓取法人與融資 (Fubon)...")
    driver = get_driver()
    
    # 1. 法人 (外資/投信)
    url_inst = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zcl/zcl.djhtm?a={stock_id}&c={start_date}&d={end_date}"
    df_inst = pd.DataFrame()
    try:
        driver.get(url_inst)
        # 等待表格出現
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//td[contains(text(),'外資買賣超')]")))
        dfs = pd.read_html(StringIO(driver.page_source))
        for df in dfs:
            if df.astype(str).apply(lambda x: x.str.contains('外資買賣超', na=False)).any().any():
                # 清理並選取需要的欄位 (日期, 外資, 投信, 自營)
                temp = df.iloc[:, [0,1,2,3]].copy()
                temp.columns = ['DateStr', '外資', '投信', '自營商']
                temp = temp[temp['DateStr'].apply(is_roc_date)] # 過濾非日期列
                df_inst = temp
                break
    except Exception as e:
        print(f"⚠️ 法人數據抓取異常: {e}")

    # 2. 融資
    url_margin = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zcn/zcn.djhtm?a={stock_id}&c={start_date}&d={end_date}"
    df_margin = pd.DataFrame()
    try:
        driver.get(url_margin)
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//td[contains(text(),'融資餘額')]")))
        dfs = pd.read_html(StringIO(driver.page_source))
        for df in dfs:
            if df.astype(str).apply(lambda x: x.str.contains('融資餘額', na=False)).any().any():
                # 欄位通常是: 日期(0), 買進, 賣出, 現償, 餘額(4), 增減(5)...
                temp = df.iloc[:, [0, 4]].copy()
                temp.columns = ['DateStr', '融資餘額']
                temp = temp[temp['DateStr'].apply(is_roc_date)]
                df_margin = temp
                break
    except Exception as e:
        print(f"⚠️ 融資數據抓取異常: {e}")
        
    driver.quit()
    
    # 處理數據轉換
    data_frames = [df_inst, df_margin]
    clean_dfs = []
    
    for d in data_frames:
        if not d.empty:
            # 轉換日期
            d['DateStr'] = d['DateStr'].apply(roc_to_datestr)
            # 轉換數值 (移除逗號)
            for col in d.columns:
                if col != 'DateStr':
                    d[col] = pd.to_numeric(d[col].astype(str).str.replace(',', '').str.replace('+', ''), errors='coerce').fillna(0)
            d = d.set_index('DateStr')
            clean_dfs.append(d)
            
    if not clean_dfs:
        return pd.DataFrame()
        
    # 合併
    result = pd.concat(clean_dfs, axis=1)
    return result

def get_broker_diff_wantgoo(stock_id):
    """從玩股網抓取 買賣家數差"""
    print(f"[{stock_id}] 3. 抓取買賣家數差 (Wantgoo)...")
    driver = get_driver()
    url = f"https://www.wantgoo.com/stock/{stock_id}/major-investors/main-trend"
    
    df_diff = pd.DataFrame()
    try:
        driver.get(url)
        time.sleep(3) # 等待 JS 載入
        dfs = pd.read_html(StringIO(driver.page_source))
        
        for df in dfs:
            cols = [str(c) for c in df.columns]
            # 玩股網的表格通常包含 '日期' 和 '家數差' 相關字眼
            if any("日期" in c for c in cols) and any("家數差" in c for c in cols):
                # 重新命名
                df.columns = [c if isinstance(c, str) else str(c) for c in df.columns]
                date_col = next((c for c in df.columns if '日期' in c), None)
                diff_col = next((c for c in df.columns if '家數差' in c), None)
                
                if date_col and diff_col:
                    temp = df[[date_col, diff_col]].copy()
                    temp.columns = ['DateStr', '家數差']
                    # Wantgoo 格式通常是 YYYY/MM/DD
                    temp['DateStr'] = pd.to_datetime(temp['DateStr']).dt.strftime('%Y-%m-%d')
                    temp['家數差'] = pd.to_numeric(temp['家數差'], errors='coerce').fillna(0)
                    temp = temp.set_index('DateStr')
                    df_diff = temp
                    break
    except Exception as e:
        print(f"⚠️ 家數差抓取異常: {e}")
        
    driver.quit()
    return df_diff

# ================= 3. 繪圖核心 =================

def create_chart(stock_id, df_final):
    # 切片取最後 70 根 K 線
    df_plot = df_final.tail(70).copy()
    if df_plot.empty: return None

    # 設定 K 線樣式
    mc = mpf.make_marketcolors(
        up=COLOR_UP, down=COLOR_DOWN, 
        edge={'up': COLOR_UP, 'down': COLOR_DOWN}, 
        wick={'up': COLOR_UP, 'down': COLOR_DOWN}, 
        volume={'up': COLOR_UP, 'down': COLOR_DOWN},
        ohlc='black'
    )
    # 使用系統找到的中文字型
    font_name = plt.rcParams['font.sans-serif'][0]
    s = mpf.make_mpf_style(base_mpf_style='yahoo', marketcolors=mc, rc={'font.family': font_name})

    # --- 定義副圖 ---
    addplots = []
    
    # 顏色邏輯函數
    def get_bar_colors(series, invert=False):
        if invert:
            # 家數差：負數(籌碼集中)用紅色(好)，正數(籌碼發散)用綠色(壞)
            return [COLOR_UP if v < 0 else COLOR_DOWN for v in series]
        else:
            return [COLOR_UP if v > 0 else COLOR_DOWN for v in series]

    # [Panel 0 - 主圖] MA & BB
    addplots.append(mpf.make_addplot(df_plot['MA5'], color='#1f77b4', width=1.0, panel=0))
    addplots.append(mpf.make_addplot(df_plot['MA10'], color='#ff7f0e', width=1.0, panel=0))
    addplots.append(mpf.make_addplot(df_plot['MA20'], color='#2ca02c', width=1.0, panel=0))
    addplots.append(mpf.make_addplot(df_plot['MA60'], color='blue', width=1.0, panel=0))
    addplots.append(mpf.make_addplot(df_plot['BB_Up'], color='gray', linestyle='--', width=0.8, panel=0))
    addplots.append(mpf.make_addplot(df_plot['BB_Low'], color='gray', linestyle='--', width=0.8, panel=0))

    # [Panel 1] 外資 (Bar)
    if '外資' in df_plot.columns:
        addplots.append(mpf.make_addplot(df_plot['外資'], type='bar', color=get_bar_colors(df_plot['外資']), panel=1, ylabel='外資'))

    # [Panel 2] 投信 (Bar)
    if '投信' in df_plot.columns:
        addplots.append(mpf.make_addplot(df_plot['投信'], type='bar', color=get_bar_colors(df_plot['投信']), panel=2, ylabel='投信'))

    # [Panel 3] 融資餘額 (Line/Area)
    if '融資餘額' in df_plot.columns:
        # 融資餘額畫線比較清楚
        addplots.append(mpf.make_addplot(df_plot['融資餘額'], color='#8e24aa', width=1.5, panel=3, ylabel='融資餘額'))

    # [Panel 4] 買賣家數差 (Bar) - 顏色反轉邏輯
    if '家數差' in df_plot.columns:
        addplots.append(mpf.make_addplot(df_plot['家數差'], type='bar', color=get_bar_colors(df_plot['家數差'], invert=True), panel=4, ylabel='家數差'))

    # --- 繪圖 ---
    output_path = "stock_report.png"
    # 版面比例: 主圖 4，副圖各 1
    ratios = (4, 1, 1, 1, 1)
    
    fig, axes = mpf.plot(
        df_plot, type='candle', style=s, volume=False, # 關閉預設 Volume
        addplot=addplots, panel_ratios=ratios,
        figsize=(12, 18), returnfig=True, tight_layout=True,
        scale_padding={'left': 0.8, 'top': 3, 'right': 1.2, 'bottom': 1}
    )
    
    ax_main = axes[0]

    # --- 1. 黃色標題區 ---
    last_date = df_plot.index[-1].strftime('%Y/%m/%d')
    title_text = f"全新 ({stock_id}) 技術分析圖"
    
    rect = patches.FancyBboxPatch((0.35, 1.05), 0.3, 0.05, boxstyle="round,pad=0.02", 
                                  fc="#FFEB3B", ec="none", transform=ax_main.transAxes, clip_on=False, zorder=10)
    ax_main.add_patch(rect)
    ax_main.text(0.5, 1.075, title_text, transform=ax_main.transAxes, fontsize=16, 
                 fontweight='bold', ha='center', va='center', color='black', zorder=11)
    
    ax_main.text(1.0, 1.08, f"Data Date: {last_date}", transform=ax_main.transAxes, 
                 fontsize=10, ha='right', color='gray')

    # --- 2. 左上角資訊框 (Info Box) ---
    last_bar = df_plot.iloc[-1]
    prev_bar = df_plot.iloc[-2]
    change = last_bar['Close'] - prev_bar['Close']
    pct_change = (change / prev_bar['Close']) * 100
    bb_w = last_bar['BB_Width'] if not pd.isna(last_bar['BB_Width']) else 0
    
    info_text = (
        f"{last_date}\n"
        f"開 {last_bar['Open']:.2f}\n"
        f"高 {last_bar['High']:.2f}\n"
        f"低 {last_bar['Low']:.2f}\n"
        f"收 {last_bar['Close']:.2f}\n"
        f"漲跌 {change:+.2f}\n"
        f"幅度 {pct_change:+.2f}%\n"
        f"量 {int(last_bar['Volume']):,}\n"
        f"布林寬比 {bb_w:.2f}%"
    )
    
    box_props = dict(boxstyle='round,pad=0.5', facecolor='white', alpha=0.9, edgecolor='gray')
    ax_main.text(0.03, 0.95, info_text, transform=ax_main.transAxes, fontsize=11,
                 verticalalignment='top', bbox=box_props, zorder=9)

    # --- 3. 背景成交量分佈 (Volume Profile) ---
    # 計算並繪製在主圖背景
    if 'Volume' in df_plot.columns:
        price_min = df_plot['Low'].min()
        price_max = df_plot['High'].max()
        bins = 50
        hist, bin_edges = np.histogram(df_plot['Close'], bins=bins, weights=df_plot['Volume'])
        y_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
        heights = (bin_edges[1] - bin_edges[0]) * 0.8
        
        max_h = hist.max()
        if max_h > 0:
            width_scale = (len(df_plot) * 0.45) / max_h
            # 使用淡藍色 (#B0E0E6)
            ax_main.barh(y_centers, hist * width_scale, height=heights, left=0, 
                         color='#B0E0E6', alpha=0.4, zorder=0, align='center')

    # 存檔
    fig.savefig(output_path, bbox_inches='tight', dpi=100)
    plt.close(fig)
    print(f"✅ 圖表已生成: {output_path}")
    return output_path

# ================= 4. 主程式 =================

if __name__ == "__main__":
    print(f"🚀 開始執行: {STOCK_ID}")
    
    # 1. 抓取股價
    df_price = get_stock_price(STOCK_ID)
    if df_price is None:
        sys.exit("❌ 無法取得股價，程式終止")

    # 設定爬蟲日期範圍 (抓過去 1 年以確保有足夠數據)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365)
    s_str = (start_date.year - 1911) if start_date.year > 1911 else start_date.year
    e_str = (end_date.year - 1911) if end_date.year > 1911 else end_date.year
    # 富邦格式通常為民國年 112/01/01
    fubon_start = f"{s_str}/{start_date.month:02d}/{start_date.day:02d}"
    fubon_end = f"{e_str}/{end_date.month:02d}/{end_date.day:02d}"

    # 2. 抓取籌碼數據
    df_chips = get_chips_fubon(STOCK_ID, fubon_start, fubon_end)
    df_diff = get_broker_diff_wantgoo(STOCK_ID)

    # 3. 合併數據
    # 以股價的 index 為主
    df_final = df_price.copy()
    
    if not df_chips.empty:
        df_chips.index = pd.to_datetime(df_chips.index)
        df_final = df_final.join(df_chips, how='left')

    if not df_diff.empty:
        df_diff.index = pd.to_datetime(df_diff.index)
        # 防止重複欄位
        if '家數差' in df_final.columns:
            df_final = df_final.drop(columns=['家數差'])
        df_final = df_final.join(df_diff, how='left')

    # 補 0 處理 (避免繪圖錯誤)
    cols_to_fill = ['外資', '投信', '融資餘額', '家數差']
    for c in cols_to_fill:
        if c not in df_final.columns:
            df_final[c] = 0
        df_final[c] = df_final[c].fillna(0)

    # 4. 繪圖
    img_path = create_chart(STOCK_ID, df_final)

    # 5. 發送 Webhook
    if WEBHOOK_URL and img_path:
        print("📤 正在發送 Discord...")
        try:
            with open(img_path, "rb") as f:
                payload = {
                    "content": f"📊 **{STOCK_ID} 技術籌碼分析週報**\n包含：外資、投信、融資餘額、買賣家數差"
                }
                files = {"file": (img_path, f, "image/png")}
                requests.post(WEBHOOK_URL, data=payload, files=files)
            print("✅ Discord 發送成功")
        except Exception as e:
            print(f"❌ Discord 發送失敗: {e}")
    else:
        print("⚠️ 未設定 Webhook 或圖片生成失敗，跳過發送。")
