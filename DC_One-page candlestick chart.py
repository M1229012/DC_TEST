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
import matplotlib.gridspec as gridspec
from matplotlib import font_manager
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
STOCK_ID = "2455" # 預設改為你圖片中的 2455 全新，確認格式用
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK")

# 定義顏色 (符合看盤軟體習慣)
COLOR_UP = '#ef5350'   # 紅 (漲)
COLOR_DOWN = '#26a69a' # 綠 (跌)
COLOR_TEXT = 'black'   # 文字黑
COLOR_BG = 'white'     # 背景白

# 設定中文字型
plt.rcParams['font.sans-serif'] = ['WenQuanYi Zen Hei', 'Microsoft JhengHei', 'SimHei']
plt.rcParams['axes.unicode_minus'] = False

# ================= 1. 爬蟲與數據處理 (邏輯保留) =================

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

def calculate_indicators(df):
    df = df.copy()
    # MA
    df['MA5'] = df['Close'].rolling(5).mean()
    df['MA10'] = df['Close'].rolling(10).mean()
    df['MA20'] = df['Close'].rolling(20).mean()
    df['MA60'] = df['Close'].rolling(60).mean()
    # BB
    df['BB_Mid'] = df['MA20']
    df['BB_Std'] = df['Close'].rolling(20).std()
    df['BB_Up'] = df['BB_Mid'] + 2 * df['BB_Std']
    df['BB_Low'] = df['BB_Mid'] - 2 * df['BB_Std']
    # KD
    rsv_period = 9
    df['9_High'] = df['High'].rolling(9).max()
    df['9_Low'] = df['Low'].rolling(9).min()
    df['RSV'] = 100 * ((df['Close'] - df['9_Low']) / (df['9_High'] - df['9_Low'])).fillna(50)
    k, d = [50], [50]
    for r in df['RSV'].tolist()[1:]:
        k.append(k[-1]*2/3 + r*1/3)
        d.append(d[-1]*2/3 + k[-1]*1/3)
    df['K'], df['D'] = pd.Series(k, index=df.index), pd.Series(d, index=df.index)
    df['J'] = 3 * df['K'] - 2 * df['D']
    
    return df

def get_driver():
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--window-size=1920,1080')
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)

def get_stock_data(stock_id):
    print(f"[{stock_id}] 1. 抓取股價 (Yahoo)...")
    try:
        df = yf.Ticker(f"{stock_id}.TW").history(period="1y")
        if df.empty: df = yf.Ticker(f"{stock_id}.TWO").history(period="1y")
        if df.empty: return None
        df['Volume'] = df['Volume'] / 1000 # 轉張數
        df.index = df.index.tz_localize(None)
        df['DateStr'] = df.index.strftime('%Y-%m-%d')
        return calculate_indicators(df)
    except Exception as e:
        print(f"Error fetching price: {e}")
        return None

def get_fubon_chips(stock_id, s_date, e_date):
    print(f"[{stock_id}] 2. 抓取籌碼 (Fubon)...")
    driver = get_driver()
    data = {'inst': None, 'margin': None}
    
    # 法人
    try:
        url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zcl/zcl.djhtm?a={stock_id}&c={s_date}&d={e_date}"
        driver.get(url)
        WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "//td[contains(text(),'外資買賣超')]")))
        dfs = pd.read_html(StringIO(driver.page_source))
        for df in dfs:
            if df.astype(str).apply(lambda x: x.str.contains('外資買賣超', na=False)).any().any():
                clean = df.iloc[:, [0,1,2,3]].copy()
                clean.columns = ['DateStr', '外資', '投信', '自營商']
                clean = clean[clean['DateStr'].apply(is_roc_date)]
                for c in clean.columns[1:]:
                    clean[c] = pd.to_numeric(clean[c].astype(str).str.replace(',','').str.replace('+',''), errors='coerce').fillna(0)
                clean['DateStr'] = clean['DateStr'].apply(roc_to_datestr)
                data['inst'] = clean
    except: pass

    # 融資券
    try:
        url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zcn/zcn.djhtm?a={stock_id}&c={s_date}&d={e_date}"
        driver.get(url)
        time.sleep(1)
        dfs = pd.read_html(StringIO(driver.page_source))
        for df in dfs:
            if df.astype(str).apply(lambda x: x.str.contains('融資餘額', na=False)).any().any():
                clean = df.iloc[:, [0,4,5,11,12]].copy()
                clean.columns = ['DateStr', '融資餘額', '融資增減', '融券餘額', '融券增減']
                clean = clean[clean['DateStr'].apply(is_roc_date)]
                for c in clean.columns[1:]:
                    clean[c] = pd.to_numeric(clean[c].astype(str).str.replace(',','').str.replace('+',''), errors='coerce').fillna(0)
                clean['DateStr'] = clean['DateStr'].apply(roc_to_datestr)
                data['margin'] = clean
    except: pass
    
    driver.quit()
    return data

def get_wantgoo_diff(stock_id):
    # 針對 Wantgoo 的家數差抓取 (改用標準 Selenium 模擬)
    print(f"[{stock_id}] 3. 抓取家數差 (Wantgoo)...")
    driver = get_driver()
    try:
        url = f"https://www.wantgoo.com/stock/{stock_id}/major-investors/main-trend"
        driver.get(url)
        time.sleep(3) # 等待 Cloudflare/JS
        html = driver.page_source
        dfs = pd.read_html(StringIO(html))
        for df in dfs:
            cols = [str(c) for c in df.columns]
            if any("家數差" in c for c in cols) and any("日期" in c for c in cols):
                # 找到目標
                df = df.rename(columns={c: 'DateStr' for c in df.columns if '日期' in str(c)})
                target_col = next((c for c in df.columns if '家數差' in str(c)), None)
                if target_col:
                    clean = df[['DateStr', target_col]].copy()
                    clean.columns = ['DateStr', '家數差']
                    clean['DateStr'] = pd.to_datetime(clean['DateStr']).dt.strftime('%Y-%m-%d')
                    clean['家數差'] = pd.to_numeric(clean['家數差'], errors='coerce').fillna(0)
                    driver.quit()
                    return clean
    except: pass
    driver.quit()
    return None

# ================= 2. 繪圖核心 (MPLFinance 客製化) =================

def plot_dashboard(stock_id, df_final):
    # 確保只有 70 根 K 棒
    df_plot = df_final.tail(70).copy()
    if df_plot.empty: return None
    
    # 準備樣式
    mc = mpf.make_marketcolors(up='r', down='g', edge={'up':'r','down':'g'}, wick={'up':'r','down':'g'}, volume={'up':'r','down':'g'})
    s = mpf.make_mpf_style(base_mpf_style='yahoo', marketcolors=mc, rc={'font.family': 'WenQuanYi Zen Hei', 'axes.unicode_minus': False})

    # ------------------ 設定副圖資料 (AddPlots) ------------------
    addplots = []
    
    # 1. 主圖指標 (MA & BB)
    addplots.append(mpf.make_addplot(df_plot['MA5'], color='blue', width=1, panel=0))
    addplots.append(mpf.make_addplot(df_plot['MA10'], color='orange', width=1, panel=0))
    addplots.append(mpf.make_addplot(df_plot['MA20'], color='green', width=1, panel=0))
    addplots.append(mpf.make_addplot(df_plot['BB_Up'], color='gray', linestyle='--', width=0.8, panel=0))
    addplots.append(mpf.make_addplot(df_plot['BB_Low'], color='gray', linestyle='--', width=0.8, panel=0))

    # 2. 三大法人 (Bar + Line) - Panel 2
    inst_colors = ['r' if v >= 0 else 'g' for v in df_plot['三大法人']]
    addplots.append(mpf.make_addplot(df_plot['三大法人'], type='bar', color=inst_colors, panel=2, ylabel='三大法人'))
    addplots.append(mpf.make_addplot(df_plot['三大法人_Cum'], color='blue', width=1.5, panel=2))

    # 3. 外資 (Bar + Line) - Panel 3
    foreign_colors = ['r' if v >= 0 else 'g' for v in df_plot['外資']]
    addplots.append(mpf.make_addplot(df_plot['外資'], type='bar', color=foreign_colors, panel=3, ylabel='外資'))
    addplots.append(mpf.make_addplot(df_plot['外資_Cum'], color='blue', width=1.5, panel=3))

    # 4. 投信 (Bar + Line) - Panel 4
    trust_colors = ['r' if v >= 0 else 'g' for v in df_plot['投信']]
    addplots.append(mpf.make_addplot(df_plot['投信'], type='bar', color=trust_colors, panel=4, ylabel='投信'))
    addplots.append(mpf.make_addplot(df_plot['投信_Cum'], color='blue', width=1.5, panel=4))

    # 5. 自營商 (Bar Only) - Panel 5
    dealer_colors = ['r' if v >= 0 else 'g' for v in df_plot['自營商']]
    addplots.append(mpf.make_addplot(df_plot['自營商'], type='bar', color=dealer_colors, panel=5, ylabel='自營商'))

    # 6. 融資餘額 (Line) + 融資增減 (Bar) - Panel 6
    # 這裡依照圖片習慣，餘額用線，增減用柱狀
    margin_colors = ['r' if v >= 0 else 'g' for v in df_plot['融資增減']]
    addplots.append(mpf.make_addplot(df_plot['融資增減'], type='bar', color=margin_colors, panel=6, ylabel='融資'))
    addplots.append(mpf.make_addplot(df_plot['融資餘額'], color='orange', width=1.5, panel=6, secondary_y=False)) 

    # 7. 家數差 (Bar) - Panel 7
    # 負數(集中)為紅，正數(分散)為綠
    diff_colors = ['r' if v < 0 else 'g' for v in df_plot['家數差']]
    addplots.append(mpf.make_addplot(df_plot['家數差'], type='bar', color=diff_colors, panel=7, ylabel='家數差'))

    # ------------------ 繪圖與後處理 ------------------
    output_path = "dashboard.png"
    
    # 使用 returnfig=True 取得 figure 和 axes 以便手繪 Volume Profile
    fig, axes = mpf.plot(
        df_plot, 
        type='candle', 
        style=s, 
        volume=True, 
        addplot=addplots,
        panel_ratios=(4, 1, 1, 1, 1, 1, 1, 1), # 調整比例
        title=dict(title=f"{stock_id} 技術分析圖", size=18, weight='bold'),
        figsize=(12, 22), 
        returnfig=True,
        tight_layout=True
    )

    # --- 繪製 K 線上的量價分佈 (Volume Profile) ---
    # 邏輯：計算區間內每個價格的成交量總和
    ax_main = axes[0]
    
    # 1. 計算 Volume Profile
    price_bins = 50
    min_p = df_plot['Low'].min()
    max_p = df_plot['High'].max()
    bin_width = (max_p - min_p) / price_bins
    
    # 建立價格區間
    bins = np.linspace(min_p, max_p, price_bins + 1)
    vol_profile = np.zeros(price_bins)
    
    # 將每一根 K 棒的量分配到它經過的價格區間 (簡易版：分配到 (High+Low)/2 的區間)
    # 為了更精確，我們假設量均勻分佈在 High-Low 之間 (若 High=Low 則全部分配)
    for i, row in df_plot.iterrows():
        v = row['Volume']
        h, l = row['High'], row['Low']
        if h == l:
            idx = int((h - min_p) / bin_width)
            if 0 <= idx < price_bins: vol_profile[idx] += v
        else:
            # 涉及的 bins
            idx_start = int((l - min_p) / bin_width)
            idx_end = int((h - min_p) / bin_width)
            idx_start = max(0, idx_start)
            idx_end = min(price_bins - 1, idx_end)
            if idx_end >= idx_start:
                v_per_bin = v / (idx_end - idx_start + 1)
                vol_profile[idx_start : idx_end+1] += v_per_bin

    # 2. 決定顏色 (第一大量 紅橙，第二大量 橘，其他 淺藍/灰)
    sorted_indices = np.argsort(vol_profile)[::-1] # 降冪排序
    colors = ['#B0C4DE'] * price_bins # 預設 LightSteelBlue (淺藍灰)
    
    if len(sorted_indices) > 0: colors[sorted_indices[0]] = '#FF4500' # OrangeRed (紅橙)
    if len(sorted_indices) > 1: colors[sorted_indices[1]] = '#FFA500' # Orange (橘)
    
    # 3. 畫在主圖上 (barh)
    # 為了不遮擋 K 線，設定 alpha 和 zorder，並限制長度
    max_vol = np.max(vol_profile)
    # 讓最長的 bar 佔畫面寬度的 1/3
    x_len = len(df_plot)
    scale_factor = (x_len * 0.4) / max_vol 
    
    # 由於 mplfinance x軸是 0..N，我們從左邊 (0) 開始畫
    # 這裡的 y 是價格 (bins中心)
    bin_centers = (bins[:-1] + bins[1:]) / 2
    ax_main.barh(bin_centers, vol_profile * scale_factor, height=bin_width*0.8, left=0, color=colors, alpha=0.4, zorder=0)

    # 存檔
    fig.savefig(output_path, bbox_inches='tight')
    plt.close(fig)
    return output_path

def send_discord(img_path):
    if not WEBHOOK_URL:
        print("❌ 未設定 Webhook")
        return
    try:
        with open(img_path, "rb") as f:
            payload = {"content": f"📊 **{STOCK_ID} 戰情分析 (Github Actions)**"}
            files = {"file": (img_path, f, "image/png")}
            requests.post(WEBHOOK_URL, data=payload, files=files)
            print("✅ 發送成功")
    except Exception as e:
        print(f"❌ 發送失敗: {e}")

# ================= 主程式 =================
if __name__ == "__main__":
    print(f"🚀 啟動分析: {STOCK_ID}")
    
    # 1. 抓資料
    s_date, e_date = (datetime.now() - timedelta(days=200)).strftime('%Y-%m-%d'), datetime.now().strftime('%Y-%m-%d')
    
    df = get_stock_data(STOCK_ID)
    if df is None: sys.exit("無法取得股價")
    
    chips = get_fubon_chips(STOCK_ID, s_date, e_date)
    wg_diff = get_wantgoo_diff(STOCK_ID)
    
    # 2. 合併資料
    df.index = pd.to_datetime(df['DateStr'])
    
    # 合併籌碼
    if chips['inst'] is not None:
        inst = chips['inst'].set_index('DateStr')
        inst.index = pd.to_datetime(inst.index)
        df = df.join(inst, how='left')
    
    if chips['margin'] is not None:
        mar = chips['margin'].set_index('DateStr')
        mar.index = pd.to_datetime(mar.index)
        df = df.join(mar, how='left')
        
    if wg_diff is not None:
        wg = wg_diff.set_index('DateStr')
        wg.index = pd.to_datetime(wg.index)
        df = df.join(wg, how='left')
        
    # 3. 填補空值與計算
    cols_to_fix = ['外資', '投信', '自營商', '融資餘額', '融資增減', '家數差']
    for c in cols_to_fix:
        if c not in df.columns: df[c] = 0
        else: df[c] = df[c].fillna(0)
        
    df['三大法人'] = df['外資'] + df['投信'] + df['自營商']
    
    # 計算累計值 (Line Chart 用)
    df['三大法人_Cum'] = df['三大法人'].cumsum()
    df['外資_Cum'] = df['外資'].cumsum()
    df['投信_Cum'] = df['投信'].cumsum()
    
    # 4. 繪圖與發送
    img = plot_dashboard(STOCK_ID, df)
    if img: send_discord(img)
