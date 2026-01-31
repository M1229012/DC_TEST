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
import shutil

# ================= 設定區 =================
STOCK_ID = "2313"
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_TEST")

# 顏色定義
COLOR_UP = '#ef5350'   # 紅
COLOR_DOWN = '#26a69a' # 綠

# 設定中文字型
plt.rcParams['font.sans-serif'] = ['WenQuanYi Zen Hei', 'Microsoft JhengHei', 'SimHei', 'Arial']
plt.rcParams['axes.unicode_minus'] = False

# ================= 1. 輔助函式 =================

def is_roc_date(s: str) -> bool:
    """檢查是否為民國日期格式"""
    return re.match(r"\d{2,3}/\d{1,2}/\d{1,2}", str(s).strip()) is not None

def roc_to_datestr(d_str: str):
    """將民國日期轉為西元日期字串"""
    parts = re.split(r"[/-]", str(d_str).strip())
    if len(parts) < 2: 
        return None
    y = int(parts[0])
    y = y + 1911 if y < 1911 else y
    m = int(parts[1])
    d = int(parts[2]) if len(parts) > 2 else 1
    return f"{y:04d}-{m:02d}-{d:02d}"

def calculate_technical_indicators(df):
    """計算技術指標"""
    df = df.copy()
    
    # BB
    df['BB_Mid'] = df['Close'].rolling(window=20).mean()
    df['BB_Std'] = df['Close'].rolling(window=20).std()
    df['BB_Up'] = df['BB_Mid'] + 2 * df['BB_Std']
    df['BB_Low'] = df['BB_Mid'] - 2 * df['BB_Std']

    # KDJ
    rsv_period = 9
    df['9_High'] = df['High'].rolling(window=rsv_period).max()
    df['9_Low'] = df['Low'].rolling(window=rsv_period).min()
    df['RSV'] = 100 * ((df['Close'] - df['9_Low']) / (df['9_High'] - df['9_Low']))
    df['RSV'] = df['RSV'].fillna(50)
    
    k_list = []
    d_list = []
    k_prev = 50
    d_prev = 50
    for rsv in df['RSV']:
        if pd.isna(rsv):
            k_now = k_prev
            d_now = d_prev
        else:
            k_now = (2/3) * k_prev + (1/3) * rsv
            d_now = (2/3) * d_prev + (1/3) * k_now
        k_list.append(k_now)
        d_list.append(d_now)
        k_prev = k_now
        d_prev = d_now
        
    df['K'] = k_list
    df['D'] = d_list
    df['J'] = 3 * df['K'] - 2 * df['D']

    # MACD
    exp12 = df['Close'].ewm(span=12, adjust=False).mean()
    exp26 = df['Close'].ewm(span=26, adjust=False).mean()
    df['DIF'] = exp12 - exp26
    df['DEA'] = df['DIF'].ewm(span=9, adjust=False).mean()
    df['MACD_Hist'] = 2 * (df['DIF'] - df['DEA'])

    # RSI
    delta = df['Close'].diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    ema_up = up.ewm(com=5, adjust=False).mean() 
    ema_down = down.ewm(com=5, adjust=False).mean()
    rs = ema_up / ema_down
    df['RSI'] = 100 - (100 / (1 + rs))

    # MA
    df['MA5'] = df['Close'].rolling(window=5).mean()
    df['MA10'] = df['Close'].rolling(window=10).mean()
    df['MA20'] = df['Close'].rolling(window=20).mean()
    df['MA60'] = df['Close'].rolling(window=60).mean()
    df['MA120'] = df['Close'].rolling(window=120).mean()
    df['MA240'] = df['Close'].rolling(window=240).mean()
    
    df = df.replace([np.inf, -np.inf], np.nan)
    return df

# ================= 2. 爬蟲核心 (完全依照 Streamlit) =================

def get_driver():
    """
    ✅ 完全依照 Streamlit 籌碼K線的 get_driver() 
    一字不改，完整複製
    """
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    # 1. 開啟 Eager 模式 (不等待資源載入完畢)
    options.page_load_strategy = 'eager'

    # 2. 禁止圖片、CSS、通知等資源載入
    prefs = {
        "profile.managed_default_content_settings.images": 2,          # 禁止圖片
        "profile.default_content_setting_values.notifications": 2,     # 禁止通知
        "profile.managed_default_content_settings.stylesheets": 2,     # 禁止 CSS (若爬蟲報錯可註解掉這行)
        "profile.managed_default_content_settings.cookies": 2,         # 禁止 Cookies (部分網站可能會擋，可視情況開啟)
        "profile.managed_default_content_settings.javascript": 1,      # JS 建議開啟 (因為你是爬動態網頁)
        "profile.managed_default_content_settings.plugins": 1,
        "profile.managed_default_content_settings.popups": 2,
        "profile.managed_default_content_settings.geolocation": 2,
        "profile.managed_default_content_settings.media_stream": 2,
    }
    options.add_experimental_option("prefs", prefs)
    
    # 額外參數減少渲染負擔
    options.add_argument('--blink-settings=imagesEnabled=false')
    options.add_argument('--disable-extensions')
    options.add_argument('--disable-infobars')
    
    if shutil.which("chromium"):
        options.binary_location = shutil.which("chromium")
    elif shutil.which("chromium-browser"):
        options.binary_location = shutil.which("chromium-browser")
        
    if shutil.which("chromedriver"):
        service = Service(shutil.which("chromedriver"))
    else:
        service = Service(ChromeDriverManager().install())

    driver = webdriver.Chrome(service=service, options=options)
    return driver

def get_stock_data(stock_id):
    """
    ✅ 完全依照 Streamlit 的 get_stock_price()
    """
    print(f"[{stock_id}] 1. 抓取股價 (Yahoo)...")
    tickers_to_try = [f"{stock_id}.TW", f"{stock_id}.TWO"]
    df = None
    for ticker in tickers_to_try:
        try:
            stock = yf.Ticker(ticker)
            temp_df = stock.history(period="10y") 
            if not temp_df.empty:
                # ✅ 將成交量單位由「股」轉為「張」 (除以 1000)
                temp_df['Volume'] = temp_df['Volume'] / 1000
                df = temp_df
                break
        except: 
            continue
    if df is None or df.empty: 
        return None

    try:
        df.index = df.index.tz_localize(None)
        df['DateStr'] = df.index.strftime('%Y-%m-%d')
        df = calculate_technical_indicators(df)
        return df
    except: 
        return None

def get_institutional_data(stock_id, start_date, end_date):
    """
    ✅ 完全依照 Streamlit 的 get_institutional_data()
    """
    print(f"[{stock_id}] 2. 抓取法人 (Fubon)...")
    driver = get_driver()
    url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zcl/zcl.djhtm?a={stock_id}&c={start_date}&d={end_date}"
    try:
        driver.get(url)
        WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "/html/body/div[1]/table/tbody/tr[2]/td[2]/table/tbody/tr/td/form/table/tbody/tr/td/table/tbody/tr[8]/td[1]")))
        html = driver.page_source
        tables = pd.read_html(StringIO(html))
        
        target_df = None
        for df in tables:
            if df.astype(str).apply(lambda x: x.str.contains('外資買賣超', na=False)).any().any():
                target_df = df
                break
        
        if target_df is not None:
            if len(target_df.columns) >= 4:
                clean_df = target_df.iloc[:, [0, 1, 2, 3]].copy()
                clean_df.columns = ['日期', '外資買賣超', '投信買賣超', '自營商買賣超']
                
                clean_df = clean_df[clean_df['日期'].apply(is_roc_date)]
                
                for col in ['外資買賣超', '投信買賣超', '自營商買賣超']:
                    clean_df[col] = clean_df[col].astype(str).str.replace(',', '').str.replace('+', '').str.replace('nan', '0')
                    clean_df[col] = pd.to_numeric(clean_df[col], errors='coerce').fillna(0)

                clean_df['DateStr'] = clean_df['日期'].apply(roc_to_datestr)
                return clean_df.dropna(subset=['DateStr'])
    except:
        pass
    finally:
        driver.quit()
    return None

def get_margin_data(stock_id, start_date, end_date):
    """
    ✅ 完全依照 Streamlit 的 get_margin_data()
    """
    print(f"[{stock_id}] 3. 抓取融資 (Fubon)...")
    driver = get_driver()
    url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zcn/zcn.djhtm?a={stock_id}&c={start_date}&d={end_date}"
    try:
        driver.get(url)
        WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "/html/body/div[1]/table/tbody/tr[2]/td[2]/table/tbody/tr/td/form/table/tbody/tr/td/table/tbody/tr[8]/td[1]")))
        html = driver.page_source
        tables = pd.read_html(StringIO(html))
        
        target_df = None
        for df in tables:
            if df.astype(str).apply(lambda x: x.str.contains('融資餘額', na=False)).any().any():
                target_df = df
                break
        
        if target_df is not None:
            if len(target_df.columns) >= 13:
                clean_df = target_df.iloc[:, [0, 4, 5, 11, 12]].copy()
                clean_df.columns = ['日期', '融資餘額', '融資增減', '融券餘額', '融券增減']
                
                clean_df = clean_df[clean_df['日期'].apply(is_roc_date)]
                
                for col in ['融資餘額', '融資增減', '融券餘額', '融券增減']:
                    clean_df[col] = clean_df[col].astype(str).str.replace(',', '').str.replace('+', '').str.replace('nan', '0')
                    clean_df[col] = pd.to_numeric(clean_df[col], errors='coerce').fillna(0)
                
                clean_df['DateStr'] = clean_df['日期'].apply(roc_to_datestr)
                return clean_df.dropna(subset=['DateStr'])
    except:
        pass
    finally:
        driver.quit()
    return None

def get_wantgoo_data(stock_id):
    """
    ✅ 完全依照 Streamlit 但簡化 (因為 Streamlit 用 subprocess + SeleniumBase)
    這裡用標準 Selenium
    """
    print(f"[{stock_id}] 4. 抓取家數差 (Wantgoo)...")
    driver = get_driver()
    try:
        url = f"https://www.wantgoo.com/stock/{stock_id}/major-investors/main-trend"
        driver.get(url)
        time.sleep(5)  # 等待 JavaScript 載入
        
        dfs = pd.read_html(StringIO(driver.page_source))
        
        target_df = None
        for df in dfs:
            cols = [str(c).strip() for c in df.columns]
            if any("買賣超" in c for c in cols) and any("家數差" in c for c in cols):
                target_df = df
                break
        
        if target_df is not None and not target_df.empty:
            target_df.columns = [str(c).strip() for c in target_df.columns]
            buy_col = next((c for c in target_df.columns if "買賣超" in c), None)
            diff_col = next((c for c in target_df.columns if "家數差" in c), None)
            date_col = next((c for c in target_df.columns if "日期" in c), None)
            
            if buy_col and diff_col and date_col:
                cols_to_keep = [date_col, buy_col, diff_col]
                col_names = ['日期', '買賣超', '家數差']
                
                clean_df = target_df[cols_to_keep].copy()
                clean_df.columns = col_names
                
                for col in ['買賣超', '家數差']:
                    clean_df[col] = pd.to_numeric(clean_df[col].astype(str).str.replace(',', ''), errors='coerce').fillna(0)

                clean_df['dt_temp'] = pd.to_datetime(clean_df['日期'], errors='coerce')
                clean_df['dt_temp'] = clean_df['dt_temp'] + pd.Timedelta(days=1)
                
                clean_df['DateStr'] = clean_df['dt_temp'].dt.strftime('%Y-%m-%d')
                clean_df['日期'] = clean_df['DateStr'].str.replace('-', '/')
                clean_df = clean_df.drop(columns=['dt_temp'])
                
                return clean_df.sort_values('DateStr')
    except:
        pass
    finally:
        driver.quit()
    return None

# ================= 3. 繪圖核心 =================

def create_dashboard(stock_id, df_final):
    """建立綜合儀表板"""
    df_plot = df_final.tail(70).copy()
    if df_plot.empty: 
        return None
    
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
        rc={'font.family': 'WenQuanYi Zen Hei'}
    )
    
    addplots = []
    
    # Panel 0: K線 + MA + BB
    addplots.append(mpf.make_addplot(df_plot['MA5'], color='#1f77b4', width=1.2, panel=0))
    addplots.append(mpf.make_addplot(df_plot['MA10'], color='#ff7f0e', width=1.2, panel=0))
    addplots.append(mpf.make_addplot(df_plot['MA20'], color='#2ca02c', width=1.2, panel=0))
    addplots.append(mpf.make_addplot(df_plot['MA60'], color='blue', width=1.2, panel=0))
    addplots.append(mpf.make_addplot(df_plot['BB_Up'], color='gray', linestyle='--', width=0.8, panel=0))
    addplots.append(mpf.make_addplot(df_plot['BB_Low'], color='gray', linestyle='--', width=0.8, panel=0))
    
    def get_bar_colors(series): 
        return [COLOR_UP if v >= 0 else COLOR_DOWN for v in series]
    
    # Panel 2: 外資
    addplots.append(mpf.make_addplot(
        df_plot['外資買賣超'], 
        type='bar', 
        color=get_bar_colors(df_plot['外資買賣超']), 
        panel=2, 
        ylabel='外資買賣超'
    ))
    
    # Panel 3: 投信
    addplots.append(mpf.make_addplot(
        df_plot['投信買賣超'], 
        type='bar', 
        color=get_bar_colors(df_plot['投信買賣超']), 
        panel=3, 
        ylabel='投信買賣超'
    ))
    
    # Panel 4: 融資餘額
    addplots.append(mpf.make_addplot(
        df_plot['融資餘額'], 
        color='#9C27B0', 
        width=2, 
        panel=4, 
        ylabel='融資餘額'
    ))
    
    # Panel 5: 家數差
    diff_colors = [COLOR_UP if v < 0 else COLOR_DOWN for v in df_plot['家數差']]
    addplots.append(mpf.make_addplot(
        df_plot['家數差'], 
        type='bar', 
        color=diff_colors, 
        panel=5, 
        ylabel='買賣家數差'
    ))
    
    output_path = "dashboard.png"
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
    
    # 客製化標題
    ax_main = axes[0]
    last_date = df_plot.iloc[-1]['DateStr']
    last_close = df_plot.iloc[-1]['Close']
    last_change = df_plot.iloc[-1]['Close'] - df_plot.iloc[-2]['Close']
    last_change_pct = (last_change / df_plot.iloc[-2]['Close']) * 100
    
    title_text = f"{stock_id} 技術分析圖 ({last_date})"
    subtitle_text = f"收盤 {last_close:.2f} | {last_change:+.2f} ({last_change_pct:+.2f}%)"
    
    rect = patches.FancyBboxPatch(
        (0.30, 1.01), 0.40, 0.035, 
        boxstyle="round,pad=0.015", 
        fc="#FFD700", 
        ec="none", 
        transform=ax_main.transAxes, 
        clip_on=False
    )
    ax_main.add_patch(rect)
    
    ax_main.text(
        0.5, 1.028, title_text, 
        transform=ax_main.transAxes, 
        fontsize=16, 
        fontweight='bold', 
        ha='center', 
        va='center', 
        color='black'
    )
    
    ax_main.text(
        0.5, 0.98, subtitle_text,
        transform=ax_main.transAxes,
        fontsize=10,
        ha='center',
        va='top',
        color='#333333',
        bbox=dict(boxstyle='round,pad=0.5', facecolor='white', alpha=0.8, edgecolor='none')
    )
    
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
    
    for ax in axes:
        ax.grid(True, alpha=0.3, linestyle=':', linewidth=0.5)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.tick_params(labelsize=8)
    
    plt.tight_layout()
    fig.subplots_adjust(hspace=0.3, top=0.96)
    
    fig.savefig(output_path, bbox_inches='tight', dpi=120)
    plt.close(fig)
    return output_path

def send_discord(img_path):
    """發送到 Discord"""
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
    start = end - timedelta(days=730)  # ✅ 改為 2 年確保資料充足
    s_str, e_str = start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
    
    # 1. 抓資料
    df = get_stock_data(STOCK_ID)
    if df is None: 
        sys.exit("無法取得股價")
    
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
        
    # 3. 補 0
    cols = ['外資買賣超', '投信買賣超', '自營商買賣超', '融資餘額', '融資增減', '家數差']
    for c in cols:
        if c not in df.columns: 
            df[c] = 0
        df[c] = df[c].fillna(0)
    
    # 4. 生成
    img = create_dashboard(STOCK_ID, df)
    if img: 
        print(f"✅ 圖表生成: {img}")
        send_discord(img)
