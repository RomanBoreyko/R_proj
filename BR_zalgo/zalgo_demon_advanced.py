#!/usr/bin/env python3
"""
🌀 Demon Zalgo Advanced - Расширенная версия с дополнительными функциями
- Иконка в системном трее
- Продвинутое логирование
- Горячие клавиши
- Статистика использования
"""

import keyboard
import pyautogui
import random
import time
import threading
import sys
import os
import json
import logging
from datetime import datetime
import pystray
from PIL import Image, ImageDraw
import io

class ZalgoDemonAdvanced:
    def __init__(self, config_file="zalgo_config_advanced.json"):
        self.zalgo_mode = False
        self.config = self.load_config(config_file)
        self.setup_logging()
        self.running = True
        self.stats = {
            "characters_transformed": 0,
            "sessions": 0,
            "total_time_active": 0,
            "start_time": datetime.now()
        }
        self.session_start = None
        
        # Настройки pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.01
        
        # Создание иконки для трея
        self.tray_icon = None
        self.create_tray_icon()
        
        print("Demon Zalgo Advanced инициализирован...")
        self.log("Demon Zalgo Advanced запущен")
    
    def load_config(self, config_file):
        """Загрузка расширенной конфигурации"""
        default_config = {
            "intensity_min": 1,
            "intensity_max": 3,
            "log_file": "zalgo_log_advanced.txt",
            "sound_enabled": True,
            "auto_backspace": True,
            "hotkey_toggle": "ctrl+shift+z",
            "hotkey_stats": "ctrl+shift+s",
            "show_notifications": True,
            "auto_save_stats": True,
            "stats_file": "zalgo_stats.json"
        }
        
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                return {**default_config, **config}
            except Exception as e:
                print(f"⚠️ Ошибка загрузки конфига: {e}")
        
        # Создаём конфиг по умолчанию
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, indent=2, ensure_ascii=False)
        
        return default_config
    
    def setup_logging(self):
        """Настройка расширенного логирования"""
        log_file = self.config.get("log_file", "zalgo_log_advanced.txt")
        
        # Создаём форматтер с детальной информацией
        formatter = logging.Formatter(
            '%(asctime)s | %(levelname)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # Настройка файлового логгера
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(formatter)
        
        # Настройка консольного логгера
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        
        # Настройка основного логгера
        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
    
    def log(self, message, level="INFO"):
        """Расширенное логирование"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_msg = f"[{timestamp}] {message}"
        
        if level == "INFO":
            logging.info(message)
        elif level == "WARNING":
            logging.warning(message)
        elif level == "ERROR":
            logging.error(message)
        
        print(log_msg)
    
    def create_tray_icon(self):
        """Создание иконки для системного трея"""
        try:
            # Создаём простую иконку
            image = Image.new('RGB', (64, 64), color='black')
            draw = ImageDraw.Draw(image)
            
            # Рисуем символ залго
            draw.text((20, 20), "🌀", fill='white')
            
            # Создаём меню
            menu = pystray.Menu(
                pystray.MenuItem("Zalgo Status", self.show_status, default=True),
                pystray.MenuItem("Toggle Mode", self.toggle_zalgo_mode),
                pystray.MenuItem("Statistics", self.show_stats),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", self.quit_demon)
            )
            
            self.tray_icon = pystray.Icon(
                "ZalgoDemon", 
                image, 
                "Demon Zalgo Advanced",
                menu
            )
            
        except Exception as e:
            self.log(f"Ошибка создания иконки трея: {e}", "WARNING")
            self.tray_icon = None
    
    def show_status(self, icon=None, item=None):
        """Показать статус в трее"""
        status = "АКТИВЕН" if self.zalgo_mode else "НЕАКТИВЕН"
        self.notify(f"Demon Zalgo: {status}")
    
    def show_stats(self, icon=None, item=None):
        """Показать статистику"""
        uptime = datetime.now() - self.stats["start_time"]
        stats_text = f"""
🌀 Demon Zalgo Statistics
━━━━━━━━━━━━━━━━━━━━
Время работы: {uptime}
Символов преобразовано: {self.stats['characters_transformed']}
Сессий залго: {self.stats['sessions']}
Активное время: {self.stats['total_time_active']} сек
━━━━━━━━━━━━━━━━━━━━
        """
        self.notify(stats_text)
        self.log(f"Статистика запрошена: {self.stats}")
    
    def toggle_zalgo_mode(self, icon=None, item=None):
        """Переключение режима залго"""
        self.zalgo_mode = not self.zalgo_mode
        status = "🩸 АКТИВИРОВАН" if self.zalgo_mode else "💀 ДЕАКТИВИРОВАН"
        self.log(f"Zalgo режим {status} (через трей)")
        self.notify(f"Zalgo режим {status}")
        
        if self.config.get("sound_enabled", True):
            self.play_sound()
    
    def quit_demon(self, icon=None, item=None):
        """Завершение работы демона"""
        self.log("Завершение работы через трей")
        self.running = False
        if self.tray_icon:
            self.tray_icon.stop()
        sys.exit(0)
    
    def notify(self, message):
        """Показ уведомлений"""
        if self.config.get("show_notifications", True):
            try:
                if self.tray_icon:
                    self.tray_icon.notify(message, "Demon Zalgo")
            except Exception as e:
                self.log(f"Ошибка уведомления: {e}", "WARNING")
    
    def save_stats(self):
        """Сохранение статистики"""
        if self.config.get("auto_save_stats", True):
            try:
                stats_file = self.config.get("stats_file", "zalgo_stats.json")
                with open(stats_file, 'w', encoding='utf-8') as f:
                    json.dump(self.stats, f, indent=2, ensure_ascii=False)
                self.log("Статистика сохранена")
            except Exception as e:
                self.log(f"Ошибка сохранения статистики: {e}", "ERROR")
    
    def load_stats(self):
        """Загрузка статистики"""
        try:
            stats_file = self.config.get("stats_file", "zalgo_stats.json")
            if os.path.exists(stats_file):
                with open(stats_file, 'r', encoding='utf-8') as f:
                    saved_stats = json.load(f)
                    self.stats.update(saved_stats)
                self.log("Статистика загружена")
        except Exception as e:
            self.log(f"Ошибка загрузки статистики: {e}", "WARNING")
    
    def zalgoify(self, char):
        """Улучшенное преобразование символа"""
        if not char.isalpha() and char not in ".,!?;: ":
            return char
        
        original = char.upper() if char.isalpha() else char
        result = original
        
        # Добавляем залго-символы с учётом типа символа
        if char.isalpha():
            intensity = random.randint(
                self.config["intensity_min"], 
                self.config["intensity_max"]
            )
            
            for _ in range(intensity):
                zalgo_chars = "̖̗̘̙̜̝̞̟̠̤̥̦̩̪̫̬̮̯̰̱̲̳̹̺̻̼͇̍̎̄̅̿̑̐͒͗͛̋̾͌͂̓̽̚̕ͅ"
                result += random.choice(zalgo_chars)
        
        return result
    
    def watch_hotkeys(self):
        """Отслеживание горячих клавиш"""
        try:
            # Горячая клавиша для переключения режима
            keyboard.add_hotkey(
                self.config.get("hotkey_toggle", "ctrl+shift+z"),
                self.toggle_zalgo_mode
            )
            
            # Горячая клавиша для статистики
            keyboard.add_hotkey(
                self.config.get("hotkey_stats", "ctrl+shift+s"),
                self.show_stats
            )
            
            self.log(f"Горячие клавиши настроены: {self.config.get('hotkey_toggle')}, {self.config.get('hotkey_stats')}")
            
        except Exception as e:
            self.log(f"Ошибка настройки горячих клавиш: {e}", "ERROR")
    
    def watch_caps_lock(self):
        """Отслеживание CAPS LOCK с улучшенной статистикой"""
        prev_state = keyboard.is_pressed('caps lock')
        
        while self.running:
            try:
                current_state = keyboard.is_pressed('caps lock')
                
                if current_state and not prev_state:
                    self.zalgo_mode = not self.zalgo_mode
                    
                    if self.zalgo_mode:
                        self.session_start = time.time()
                        self.stats['sessions'] += 1
                    else:
                        if self.session_start:
                            session_time = time.time() - self.session_start
                            self.stats['total_time_active'] += session_time
                            self.session_start = None
                    
                    status = "🩸 АКТИВИРОВАН" if self.zalgo_mode else "💀 ДЕАКТИВИРОВАН"
                    self.log(f"Zalgo режим {status} (CAPS LOCK)")
                    self.notify(f"Zalgo режим {status}")
                    
                    if self.config.get("sound_enabled", True):
                        self.play_sound()
                    
                    time.sleep(0.5)
                
                prev_state = current_state
                time.sleep(0.1)
                
            except Exception as e:
                self.log(f"Ошибка в watch_caps_lock: {e}", "ERROR")
                time.sleep(1)
    
    def play_sound(self):
        """Улучшенное воспроизведение звука"""
        try:
            import winsound
            if self.zalgo_mode:
                # Активация - сложная последовательность
                winsound.Beep(800, 150)
                time.sleep(0.1)
                winsound.Beep(1000, 100)
            else:
                # Деактивация - простой тон
                winsound.Beep(400, 200)
        except Exception as e:
            self.log(f"Ошибка воспроизведения звука: {e}", "WARNING")
    
    def zalgo_loop(self):
        """Основной цикл с улучшенной статистикой"""
        while self.running:
            try:
                if self.zalgo_mode:
                    event = keyboard.read_event()
                    
                    if event.event_type == 'down' and len(event.name) == 1:
                        if event.name.isalnum() or event.name in ".,!?;: ":
                            if self.config.get("auto_backspace", True):
                                pyautogui.press('backspace')
                            
                            zalgo_char = self.zalgoify(event.name)
                            pyautogui.write(zalgo_char)
                            
                            # Обновляем статистику
                            self.stats['characters_transformed'] += 1
                            
                            # Логируем с уровнем детализации
                            if self.stats['characters_transformed'] % 10 == 0:
                                self.log(f"Преобразовано символов: {self.stats['characters_transformed']}")
                else:
                    time.sleep(0.1)
                    
            except Exception as e:
                self.log(f"Ошибка в zalgo_loop: {e}", "ERROR")
                time.sleep(1)
    
    def run(self):
        """Запуск расширенного демона"""
        try:
            # Загружаем статистику
            self.load_stats()
            
            # Настраиваем горячие клавиши
            self.watch_hotkeys()
            
            print("🌀 Demon Zalgo Advanced активен!")
            print("💡 Переключение: CAPS LOCK или Ctrl+Shift+Z")
            print("📊 Статистика: Ctrl+Shift+S")
            print("🛑 Для остановки: правый клик по иконке в трее")
            
            # Запускаем иконку в трее
            if self.tray_icon:
                tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
                tray_thread.start()
            
            # Запускаем отслеживание CAPS LOCK
            caps_thread = threading.Thread(target=self.watch_caps_lock, daemon=True)
            caps_thread.start()
            
            # Основной цикл
            self.zalgo_loop()
            
        except KeyboardInterrupt:
            print("\n💀 Demon Zalgo Advanced остановлен")
            self.log("Demon Zalgo Advanced остановлен пользователем")
        except Exception as e:
            print(f"\n💥 Критическая ошибка: {e}")
            self.log(f"Критическая ошибка: {e}", "ERROR")
        finally:
            self.running = False
            self.save_stats()
            if self.tray_icon:
                self.tray_icon.stop()
            print("🌀 Demon Zalgo Advanced завершил работу")

def main():
    """Точка входа для расширенной версии"""
    try:
        demon = ZalgoDemonAdvanced()
        demon.run()
    except Exception as e:
        print(f"💥 Ошибка запуска: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
