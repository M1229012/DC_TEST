import os
import sys
import re
import time
import requests
import pandas as pd
import numpy as np
import yfinance as yf
import mplfinance as mpf
import tempfile
import shutil
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
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK")  # 從 GitHub Secrets 讀取

# 定義顏色
COLOR_UP = '#ef5350' # 紅色 (上漲)
COLOR_DOWN = '#26a69a' # 綠色 (下跌)

# ================= 1. 輔助運算 (完全保留你的邏輯) =================

def is_roc_date(s: str) -> bool:
    return re.match(r"\d{2,3}/\d{1,2}/\d{1,2}", str(s).strip()) is not None

def roc_to_datestr(d_str: str):
    parts = re.split(r"[/-]", str(d_str).strip())
    if len(parts) < 2:
        return None
    y = int(parts[0])
    y = y + 1911 if y < 1911 else y
    m = int(parts[1])
    d = int(parts[2]) if len(parts) > 2 else 1
    return f"{y:04d}-{m:02d}-{d:02d}"

def calculate_technical_indicators(df):
    df = df.copy()
    
    # BB
    df['BB_Mid'] = df['Close'].rolling(window=20).mean()
    df['BB_Std'] = df['Close'].rolling(window=20).std()
    df['BB_Up'] = df['BB_Mid'] + 2 * df['BB_Std']
    df['BB_Low'] = df['BB_Mid'] - 2 * df['BB_Std']

    # KDJ (RSV, K, D, J)
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
        k_prev, d_prev = k_now, d_now
        
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

def calculate_date_range(stock_id, days):
    try:
        adj_days = days
        if days >= 120:
            adj_days = days - 1
            
        ticker = f"{stock_id}.TW"
        df = yf.Ticker(ticker).history(period=f"{max(adj_days + 60, 200)}d")
        
        if df.empty:
            ticker = f"{stock_id}.TWO"
            df = yf.Ticker(ticker).history(period=f"{max(adj_days + 60, 200)}d")
            
        if df.empty:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=adj_days * 1.5)
            return start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')
            
        df_target = df.tail(adj_days)
        start_date = df_target.index[0].strftime('%Y-%m-%d')
        end_date = df_target.index[-1].strftime('%Y-%m-%d')
        return start_date, end_date
    except:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        return start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')

# ================= 2. 爬蟲核心 (完全依照你的程式碼) =================

def get_driver_path():
    return ChromeDriverManager().install()

def get_driver():
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1920,1080')
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    # 1. 開啟 Eager 模式
    options.page_load_strategy = 'eager'

    # 2. 禁止圖片、CSS
    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.notifications": 2,
        "profile.managed_default_content_settings.stylesheets": 2,
        "profile.managed_default_content_settings.cookies": 2,
        "profile.managed_default_content_settings.javascript": 1,
        "profile.managed_default_content_settings.plugins": 1,
        "profile.managed_default_content_settings.popups": 2,
        "profile.managed_default_content_settings.geolocation": 2,
        "profile.managed_default_content_settings.media_stream": 2,
    }
    options.add_experimental_option("prefs", prefs)
    
    options.add_argument('--blink-settings=imagesEnabled=false')
    options.add_argument('--disable-extensions')
    options.add_argument('--disable-infobars')
    
    if shutil.which("chromium"):
        options.binary_location = shutil.which("chromium")
    elif shutil.which("chromium-browser"):
        options.binary_location = shutil.which("chromium-browser")
        
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    return driver

def get_institutional_data(stock_id, start_date, end_date):
    print("正在抓取法人資料...")
    driver = get_driver()
    url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zcl/zcl.djhtm?a={stock_id}&c={start_date}&d={end_date}"
    try:
        driver.get(url)
        # XPath 與你的一模一樣
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "/html/body/div[1]/table/tbody/tr[2]/td[2]/table/tbody/tr/td/form/table/tbody/tr/td/table/tbody/tr[8]/td[1]")))
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
    except Exception as e:
        print(f"法人抓取異常: {e}")
    finally:
        driver.quit()
    return None

def get_margin_data(stock_id, start_date, end_date):
    print("正在抓取融資券資料...")
    driver = get_driver()
    url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zc/zcn/zcn.djhtm?a={stock_id}&c={start_date}&d={end_date}"
    try:
        driver.get(url)
        # XPath 與你的一模一樣
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "/html/body/div[1]/table/tbody/tr[2]/td[2]/table/tbody/tr/td/form/table/tbody/tr/td/table/tbody/tr[8]/td[1]")))
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
    except Exception as e:
        print(f"融資券抓取異常: {e}")
    finally:
        driver.quit()
    return None

def get_stock_price(stock_id):
    print(f"正在抓取股價 {stock_id} ...")
    tickers_to_try = [f"{stock_id}.TW", f"{stock_id}.TWO"]
    df = None
    for ticker in tickers_to_try:
        try:
            stock = yf.Ticker(ticker)
            temp_df = stock.history(period="1y") 
            if not temp_df.empty:
                # 這裡保留你原本的邏輯
                temp_df['Volume'] = temp_df['Volume'] / 1000
                df = temp_df
                break
        except: continue
    
    if df is None or df.empty: return None

    try:
        df.index = df.index.tz_localize(None)
        df['DateStr'] = df.index.strftime('%Y-%m-%d')
        df = calculate_technical_indicators(df)
        return df
    except: return None

# ================= 3. 繪圖與發送邏輯 (修復錯誤) =================

def generate_plot(stock_id, df_price, df_inst, df_margin):
    print("開始繪製一頁式戰情圖...")
    
    # 1. 準備基礎資料 (Price)
    df = df_price.copy()
    df.index = pd.to_datetime(df['DateStr'])
    
    # 2. 合併法人資料
    if df_inst is not None:
        inst = df_inst.set_index('DateStr')
        inst.index = pd.to_datetime(inst.index)
        inst = inst[~inst.index.duplicated(keep='last')]
        df = df.join(inst[['外資買賣超', '投信買賣超', '自營商買賣超']], how='left')
        df['三大法人合計'] = df['外資買賣超'].fillna(0) + df['投信買賣超'].fillna(0) + df['自營商買賣超'].fillna(0)
    else:
        # ⚠️ CRITICAL FIX: 如果爬蟲失敗，強制補 0，避免圖表缺漏導致報錯
        print("警告: 缺失法人資料，將填補為 0 以維持圖表結構")
        df['三大法人合計'] = 0

    # 3. 合併融資資料
    if df_margin is not None:
        margin = df_margin.set_index('DateStr')
        margin.index = pd.to_datetime(margin.index)
        margin = margin[~margin.index.duplicated(keep='last')]
        df = df.join(margin[['融資增減']], how='left')
    else:
        # ⚠️ CRITICAL FIX: 如果爬蟲失敗，強制補 0
        print("警告: 缺失融資資料，將填補為 0 以維持圖表結構")
        df['融資增減'] = 0

    # 取最後 100 天
    df = df.tail(100)

    # 4. 設定圖表樣式
    mc = mpf.make_marketcolors(up='r', down='g', inherit=True)
    s = mpf.make_mpf_style(base_mpf_style='yahoo', marketcolors=mc, gridstyle=':', y_on_right=True)
    
    add_plots = []
    
    # Panel 0: 均線 & 布林
    if 'MA5' in df.columns: add_plots.append(mpf.make_addplot(df['MA5'], panel=0, color='orange', width=1))
    if 'MA20' in df.columns: add_plots.append(mpf.make_addplot(df['MA20'], panel=0, color='#ff00ff', width=1.5))
    if 'BB_Up' in df.columns: add_plots.append(mpf.make_addplot(df['BB_Up'], panel=0, color='gray', linestyle='--', width=0.8))
    if 'BB_Low' in df.columns: add_plots.append(mpf.make_addplot(df['BB_Low'], panel=0, color='gray', linestyle='--', width=0.8))

    # Panel 2: 三大法人 (即使資料是0也要畫，為了佔位)
    colors_inst = ['r' if v >= 0 else 'g' for v in df['三大法人合計'].fillna(0)]
    add_plots.append(mpf.make_addplot(df['三大法人合計'], panel=2, type='bar', color=colors_inst, ylabel='Inst Net'))

    # Panel 3: 融資 (即使資料是0也要畫)
    colors_margin = ['r' if v >= 0 else 'g' for v in df['融資增減'].fillna(0)]
    add_plots.append(mpf.make_addplot(df['融資增減'], panel=3, type='bar', color=colors_margin, ylabel='Margin'))

    # 輸出圖片
    output_filename = "dashboard.png"
    # 設定 panel_ratios = (3, 1, 1, 1) 代表: 主圖(3), 成交量(1), 法人(1), 融資(1)
    mpf.plot(
        df, 
        type='candle', 
        style=s, 
        volume=True, 
        addplot=add_plots,
        panel_ratios=(3, 1, 1, 1), 
        title=dict(title=f"{stock_id} Daily Analysis", size=20),
        figsize=(12, 16),
        savefig=dict(fname=output_filename, dpi=100, bbox_inches='tight')
    )
    return output_filename

def send_discord(img_path):
    if not WEBHOOK_URL:
        print("❌ 錯誤：未設定 Webhook URL")
        return

    print("發送至 Discord...")
    try:
        with open(img_path, "rb") as f:
            payload = {"content": f"📊 **{STOCK_ID} 籌碼戰情分析** ({datetime.now().strftime('%Y-%m-%d')})"}
            files = {"file": (img_path, f, "image/png")}
            r = requests.post(WEBHOOK_URL, data=payload, files=files)
            print(f"發送狀態: {r.status_code}")
    except Exception as e:
        print(f"發送失敗: {e}")

# ================= 主程式 =================
if __name__ == "__main__":
    print(f"啟動自動化腳本 - 目標: {STOCK_ID}")
    
    # 1. 抓股價
    df_price = get_stock_price(STOCK_ID)
    if df_price is None or df_price.empty:
        print("無法取得股價，程式結束")
        exit(1)

    # 2. 計算日期並抓取籌碼 (使用你的邏輯)
    s_d, e_d = calculate_date_range(STOCK_ID, 200)
    print(f"日期範圍: {s_d} ~ {e_d}")
    
    df_inst = get_institutional_data(STOCK_ID, s_d, e_d)
    df_margin = get_margin_data(STOCK_ID, s_d, e_d)

    # 3. 繪圖 (這裡即使 df_inst 是 None 也不會報錯了)
    img = generate_plot(STOCK_ID, df_price, df_inst, df_margin)
    
    # 4. 發送
    send_discord(img)
    print("✅ 任務完成")
