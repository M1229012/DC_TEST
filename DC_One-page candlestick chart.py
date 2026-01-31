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
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ================= 設定區 =================
STOCK_ID = "2313"
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_TEST")

# 顏色定義（台股習慣：紅漲綠跌）
COLOR_UP = '#ef5350'   # 紅
COLOR_DOWN = '#26a69a' # 綠

# 設定中文字型
plt.rcParams['font.sans-serif'] = ['WenQuanYi Zen Hei', 'Microsoft JhengHei', 'SimHei', 'Arial']
plt.rcParams['axes.unicode_minus'] = False

# ================= 1. 爬蟲與數據處理 =================
def is_roc_date(s: str) -> bool:
    return re.match(r"\d{2,3}/\d{1,2}/\d{1,2}", str(s).strip()) is not None

def roc_to_datestr(d_str: str):
    parts = re.split(r"[/-]", str(d_str).strip())
    if len(parts) < 2: return None
    y = int(parts[0])
    y = y + 1911 if y < 1911 else y
    m = int(parts[1])
    d = int(parts[2]) if len(parts) > 2 else 1
    return f"{y:04d}-{m:02d}-{d:02d}"

def calculate_technical_indicators(df):
    df = df.copy()
    # MA
    df['MA5'] = df['Close'].rolling(5).mean()
    df['MA10'] = df['Close'].rolling(10).mean()
    df['MA20'] = df['Close'].rolling(20).mean()
    df['MA60'] = df['Close'].rolling(60).mean()
    # BB
    df['BB_Mid'] = df['Close'].rolling(20).mean()
    df['BB_Std'] = df['Close'].rolling(20).std()
    df['BB_Up'] = df['BB_Mid'] + 2 * df['BB_Std']
    df['BB_Low'] = df['BB_Mid'] - 2 * df['BB_Std']
    return df

def get_driver():
    options = Options()
    # GitHub Actions 必須的參數
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    # 強力偽裝 User-Agent
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    # 指定 Chrome 路徑 (GitHub Actions 環境)
    if os.path.exists("/usr/bin/chromium-browser"):
        options.binary_location = "/usr/bin/chromium-browser"
    elif os.path.exists("/usr/bin/google-chrome"):
        options.binary_location = "/usr/bin/google-chrome"
    
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)

def get_stock_data(stock_id):
    print(f"[{stock_id}] 1. 抓取股價 (Yahoo)...")
    try:
        df = yf.Ticker(f"{stock_id}.TW").history(period="1y")
        if df.empty: df = yf.Ticker(f"{stock_id}.TWO").history(period="1y")
        if df.empty: return None
        
        df['Volume'] = df['Volume'] / 1000  # 轉張數
        df.index = df.index.tz_localize(None)
        df['DateStr'] = df.index.strftime('%Y-%m-%d')
        return calculate_technical_indicators(df)
    except Exception as e:
        print(f"Error fetching price: {e}")
        return None

def get_institutional_data(stock_id, start_date, end_date):
    print(f"[{stock_id}] 2. 抓取法人 (Fubon)...")
    driver = get_driver()
    url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zcl/zcl.djhtm?a={stock_id}&c={start_date}&d={end_date}"
    try:
        driver.get(url)
        # 等待元素，使用你原本的 XPath
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.XPATH, "//td[contains(text(),'外資買賣超')]")))
        dfs = pd.read_html(StringIO(driver.page_source))
        
        target_df = None
        for df in dfs:
            if df.astype(str).apply(lambda x: x.str.contains('外資買賣超', na=False)).any().any():
                target_df = df
                break
        
        if target_df is not None:
            clean = target_df.iloc[:, [0,1,2,3]].copy()
            clean.columns = ['DateStr', '外資', '投信', '自營商']
            clean = clean[clean['DateStr'].apply(is_roc_date)]
            for c in clean.columns[1:]:
                clean[c] = pd.to_numeric(clean[c].astype(str).str.replace(',','').str.replace('+',''), errors='coerce').fillna(0)
            clean['DateStr'] = clean['DateStr'].apply(roc_to_datestr)
            driver.quit()
            return clean.dropna(subset=['DateStr'])
    except Exception as e: 
        print(f"法人抓取失敗: {e}")
    driver.quit()
    return None

def get_margin_data(stock_id, start_date, end_date):
    print(f"[{stock_id}] 3. 抓取融資 (Fubon)...")
    driver = get_driver()
    url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zcn/zcn.djhtm?a={stock_id}&c={start_date}&d={end_date}"
    try:
        driver.get(url)
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.XPATH, "//td[contains(text(),'融資餘額')]")))
        dfs = pd.read_html(StringIO(driver.page_source))
        
        target_df = None
        for df in dfs:
            if df.astype(str).apply(lambda x: x.str.contains('融資餘額', na=False)).any().any():
                target_df = df
                break
        
        if target_df is not None:
            clean = target_df.iloc[:, [0,4,5,11,12]].copy()
            clean.columns = ['DateStr', '融資餘額', '融資增減', '融券餘額', '融券增減']
            clean = clean[clean['DateStr'].apply(is_roc_date)]
            for c in clean.columns[1:]:
                clean[c] = pd.to_numeric(clean[c].astype(str).str.replace(',','').str.replace('+',''), errors='coerce').fillna(0)
            clean['DateStr'] = clean['DateStr'].apply(roc_to_datestr)
            driver.quit()
            return clean.dropna(subset=['DateStr'])
    except Exception as e: 
        print(f"融資抓取失敗: {e}")
    driver.quit()
    return None

def get_wantgoo_data(stock_id):
    print(f"[{stock_id}] 4. 抓取家數差 (Wantgoo)...")
    driver = get_driver()
    try:
        # 使用 Selenium 直接模擬
        url = f"https://www.wantgoo.com/stock/{stock_id}/major-investors/main-trend"
        driver.get(url)
        time.sleep(5)  # 等待 JavaScript 載入
        dfs = pd.read_html(StringIO(driver.page_source))
        
        target_df = None
        for df in dfs:
            cols = [str(c) for c in df.columns]
            if any("家數差" in c for c in cols) and any("日期" in c for c in cols):
                df = df.rename(columns={c: 'DateStr' for c in df.columns if '日期' in str(c)})
                target_col = next((c for c in df.columns if '家數差' in str(c)), None)
                if target_col:
                    clean = df[['DateStr', target_col]].copy()
                    clean.columns = ['DateStr', '家數差']
                    # Wantgoo 格式通常是 YYYY/MM/DD
                    clean['DateStr'] = pd.to_datetime(clean['DateStr']).dt.strftime('%Y-%m-%d')
                    clean['家數差'] = pd.to_numeric(clean['家數差'], errors='coerce').fillna(0)
                    driver.quit()
                    return clean
    except Exception as e:
        print(f"Wantgoo 抓取失敗: {e}")
    driver.quit()
    return None

# ================= 2. 繪圖核心 =================
def create_dashboard(stock_id, df_final):
    # 切片 70 根
    df_plot = df_final.tail(70).copy()
    if df_plot.empty: return None
    
    # 設定顏色風格
    mc = mpf.make_marketcolors(
        up=COLOR_UP, down=COLOR_DOWN, 
        edge={'up': COLOR_UP, 'down': COLOR_DOWN}, 
        wick={'up': COLOR_UP, 'down': COLOR_DOWN}, 
        volume={'up': COLOR_UP, 'down': COLOR_DOWN}
    )
    s = mpf.make_mpf_style(
        base_mpf_style='yahoo', 
        marketcolors=mc, 
        gridstyle=':', 
        rc={'font.family': 'WenQuanYi Zen Hei', 'font.size': 9}
    )
    
    addplots = []
    
    # ========== Panel 0: K線圖 + MA + 布林通道 ==========
    addplots.append(mpf.make_addplot(df_plot['MA5'], color='#FF6B6B', width=1.5, panel=0))
    addplots.append(mpf.make_addplot(df_plot['MA10'], color='#FFA500', width=1.5, panel=0))
    addplots.append(mpf.make_addplot(df_plot['MA20'], color='#4ECDC4', width=1.5, panel=0))
    addplots.append(mpf.make_addplot(df_plot['MA60'], color='#95E1D3', width=1.5, panel=0))
    addplots.append(mpf.make_addplot(df_plot['BB_Up'], color='#B0B0B0', linestyle='--', width=1, panel=0))
    addplots.append(mpf.make_addplot(df_plot['BB_Low'], color='#B0B0B0', linestyle='--', width=1, panel=0))
    
    # 顏色函數
    def get_bar_colors(series): 
        return [COLOR_UP if v >= 0 else COLOR_DOWN for v in series]
    
    # ========== Panel 2: 外資買賣超 ==========
    addplots.append(mpf.make_addplot(
        df_plot['外資'], 
        type='bar', 
        color=get_bar_colors(df_plot['外資']), 
        panel=2, 
        ylabel='外資買賣超',
        alpha=0.8
    ))
    
    # ========== Panel 3: 投信買賣超 ==========
    addplots.append(mpf.make_addplot(
        df_plot['投信'], 
        type='bar', 
        color=get_bar_colors(df_plot['投信']), 
        panel=3, 
        ylabel='投信買賣超',
        alpha=0.8
    ))
    
    # ========== Panel 4: 融資餘額（折線圖）==========
    addplots.append(mpf.make_addplot(
        df_plot['融資餘額'], 
        color='#9C27B0', 
        width=2, 
        panel=4, 
        ylabel='融資餘額'
    ))
    
    # ========== Panel 5: 買賣家數差 ==========
    # 家數差：負數用紅色，正數用綠色
    diff_colors = [COLOR_UP if v < 0 else COLOR_DOWN for v in df_plot['家數差']]
    addplots.append(mpf.make_addplot(
        df_plot['家數差'], 
        type='bar', 
        color=diff_colors, 
        panel=5, 
        ylabel='買賣家數差',
        alpha=0.8
    ))
    
    output_path = "dashboard.png"
    # panel_ratios: 主圖佔 5，成交量佔 1.5，其他各佔 1
    ratios = (5, 1.5, 1, 1, 1, 1)
    
    fig, axes = mpf.plot(
        df_plot, 
        type='candle', 
        style=s, 
        volume=True, 
        addplot=addplots, 
        panel_ratios=ratios,
        figsize=(14, 18), 
        returnfig=True,
        datetime_format='%m/%d',
        xrotation=0
    )
    
    # ========== 客製化標題 ==========
    ax_main = axes[0]
    last_date = df_plot.iloc[-1]['DateStr']
    last_close = df_plot.iloc[-1]['Close']
    last_change = df_plot.iloc[-1]['Close'] - df_plot.iloc[-2]['Close']
    last_change_pct = (last_change / df_plot.iloc[-2]['Close']) * 100
    
    # 標題文字
    title_text = f"{stock_id} 技術分析圖 ({last_date})"
    subtitle_text = f"收盤 {last_close:.2f} | {last_change:+.2f} ({last_change_pct:+.2f}%)"
    
    # 黃色標題背景
    rect = patches.FancyBboxPatch(
        (0.30, 1.01), 0.40, 0.035, 
        boxstyle="round,pad=0.015", 
        fc="#FFD700", 
        ec="none", 
        transform=ax_main.transAxes, 
        clip_on=False
    )
    ax_main.add_patch(rect)
    
    # 標題文字
    ax_main.text(
        0.5, 1.028, title_text, 
        transform=ax_main.transAxes, 
        fontsize=16, 
        fontweight='bold', 
        ha='center', 
        va='center', 
        color='black'
    )
    
    # 副標題（顯示最新價格資訊）
    ax_main.text(
        0.5, 0.98, subtitle_text,
        transform=ax_main.transAxes,
        fontsize=10,
        ha='center',
        va='top',
        color='#333333',
        bbox=dict(boxstyle='round,pad=0.5', facecolor='white', alpha=0.8, edgecolor='none')
    )
    
    # ========== 圖例說明 ==========
    legend_text = "— MA5  — MA10  — MA20  — MA60  -- 布林通道"
    ax_main.text(
        0.02, 0.02, legend_text,
        transform=ax_main.transAxes,
        fontsize=8,
        ha='left',
        va='bottom',
        color='#555555',
        bbox=dict(boxstyle='round,pad=0.4', facecolor='white', alpha=0.7, edgecolor='#CCCCCC')
    )
    
    # ========== 調整各子圖樣式 ==========
    for ax in axes:
        ax.grid(True, alpha=0.3, linestyle=':', linewidth=0.5)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.tick_params(labelsize=8)
    
    # 調整佈局
    plt.tight_layout()
    fig.subplots_adjust(hspace=0.3, top=0.96)
    
    fig.savefig(output_path, bbox_inches='tight', dpi=120)
    plt.close(fig)
    return output_path

def send_discord(img_path):
    if not WEBHOOK_URL:
        print("❌ 未設定 Webhook")
        return
    try:
        with open(img_path, "rb") as f:
            payload = {"content": f"📊 {STOCK_ID} 技術分析"}
            files = {"file": (img_path, f, "image/png")}
            requests.post(WEBHOOK_URL, data=payload, files=files)
        print("✅ 發送成功")
    except Exception as e:
        print(f"❌ 發送失敗: {e}")

# ================= Main =================
if __name__ == "__main__":
    print(f"🚀 啟動: {STOCK_ID}")
    
    end = datetime.now()
    start = end - timedelta(days=365)  # 抓多一點確保資料足夠
    s_str, e_str = start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
    
    # 1. 抓資料
    df = get_stock_data(STOCK_ID)
    if df is None: sys.exit("無法取得股價")
    
    chips_inst = get_institutional_data(STOCK_ID, s_str, e_str)
    chips_margin = get_margin_data(STOCK_ID, s_str, e_str)
    chip_wantgoo = get_wantgoo_data(STOCK_ID)
    
    # 2. 合併
    df.index = pd.to_datetime(df['DateStr'])
    
    if chips_inst is not None:
        c = chips_inst.set_index('DateStr')
        c.index = pd.to_datetime(c.index)
        df = df.join(c, how='left')
    
    if chips_margin is not None:
        m = chips_margin.set_index('DateStr')
        m.index = pd.to_datetime(m.index)
        df = df.join(m, how='left')
        
    if chip_wantgoo is not None:
        w = chip_wantgoo.set_index('DateStr')
        w.index = pd.to_datetime(w.index)
        df = df.join(w, how='left')
        
    # 3. 補 0 (防止 KeyError)
    cols = ['外資', '投信', '自營商', '融資餘額', '融資增減', '家數差']
    for c in cols:
        if c not in df.columns: 
            df[c] = 0
        df[c] = df[c].fillna(0)
    
    # 4. 生成圖表
    img = create_dashboard(STOCK_ID, df)
    if img: 
        print(f"✅ 圖表生成完成: {img}")
        send_discord(img)
    else:
        print("❌ 圖表生成失敗")
