#!/usr/bin/env python3
"""
🌀 Demon Zalgo - Автономный демон искажения текста
Запускается в фоне, активируется CAPS LOCK'ом
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

# Zalgo символы для искажения
ZALGO_UP = "̍̎̄̅̿̑̐͒͗͛̋̾͌͂̓̚"
ZALGO_MID = "̕̚"
ZALGO_DOWN = "̖̗̘̙̜̝̞̟̠̤̥̦̩̪̫̬̮̯̰̱̲̳̹̺̻̼͇̽ͅ"

class ZalgoDemon:
    def __init__(self, config_file="zalgo_config.json"):
        self.zalgo_mode = False
        self.config = self.load_config(config_file)
        self.setup_logging()
        self.running = True
        
        # Настройки pyautogui
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0.01
        
        print("Demon Zalgo инициализирован...")
        self.log("Demon Zalgo запущен")
    
    def load_config(self, config_file):
        """Загрузка конфигурации"""
        default_config = {
            "intensity_min": 1,
            "intensity_max": 3,
            "log_file": "zalgo_log.txt",
            "sound_enabled": True,
            "auto_backspace": True
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
        """Настройка логирования"""
        log_file = self.config.get("log_file", "zalgo_log.txt")
        logging.basicConfig(
            filename=log_file,
            level=logging.INFO,
            format='%(asctime)s - %(message)s',
            encoding='utf-8'
        )
    
    def log(self, message):
        """Логирование событий"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_msg = f"[{timestamp}] {message}"
        print(log_msg)
        logging.info(message)
    
    def zalgoify(self, char):
        """Преобразование символа в залго-версию"""
        if not char.isalpha() and char not in ".,!?;:":
            return char
        
        # Сохраняем оригинальный символ
        original = char.upper() if char.isalpha() else char
        
        # Добавляем залго-символы
        result = original
        intensity = random.randint(
            self.config["intensity_min"], 
            self.config["intensity_max"]
        )
        
        for _ in range(intensity):
            zalgo_chars = ZALGO_UP + ZALGO_MID + ZALGO_DOWN
            result += random.choice(zalgo_chars)
        
        return result
    
    def watch_caps_lock(self):
        """Отслеживание состояния CAPS LOCK"""
        prev_state = keyboard.is_pressed('caps lock')
        
        while self.running:
            try:
                current_state = keyboard.is_pressed('caps lock')
                
                # Переключение режима при изменении состояния CAPS LOCK
                if current_state and not prev_state:
                    self.zalgo_mode = not self.zalgo_mode
                    status = "АКТИВИРОВАН" if self.zalgo_mode else "ДЕАКТИВИРОВАН"
                    self.log(f"Zalgo режим {status}")
                    
                    # Звуковое уведомление
                    if self.config.get("sound_enabled", True):
                        self.play_sound()
                    
                    time.sleep(0.5)  # Защита от повторных срабатываний
                
                prev_state = current_state
                time.sleep(0.1)
                
            except Exception as e:
                self.log(f"Ошибка в watch_caps_lock: {e}")
                time.sleep(1)
    
    def play_sound(self):
        """Воспроизведение звука (простой beep)"""
        try:
            import winsound
            if self.zalgo_mode:
                winsound.Beep(800, 200)  # Высокий тон для активации
            else:
                winsound.Beep(400, 200)  # Низкий тон для деактивации
        except ImportError:
            # Если winsound недоступен, используем системный звук
            os.system('echo \a')
        except Exception as e:
            self.log(f"Ошибка воспроизведения звука: {e}")
    
    def zalgo_loop(self):
        """Основной цикл перехвата клавиш"""
        while self.running:
            try:
                if self.zalgo_mode:
                    # Перехватываем нажатия клавиш
                    event = keyboard.read_event()
                    
                    if event.event_type == 'down' and len(event.name) == 1:
                        # Обрабатываем только печатные символы
                        if event.name.isalnum() or event.name in ".,!?;: ":
                            # Удаляем оригинальный символ (backspace)
                            if self.config.get("auto_backspace", True):
                                pyautogui.press('backspace')
                            
                            # Вводим залго-версию
                            zalgo_char = self.zalgoify(event.name)
                            pyautogui.write(zalgo_char)
                            
                            # Логируем преобразование
                            self.log(f"Преобразовано: '{event.name}' → '{zalgo_char}'")
                else:
                    time.sleep(0.1)
                    
            except Exception as e:
                self.log(f"Ошибка в zalgo_loop: {e}")
                time.sleep(1)
    
    def run(self):
        """Запуск демона"""
        try:
            print("Demon Zalgo активен!")
            print("Переключение режима: CAPS LOCK")
            print("Для остановки нажмите Ctrl+C")
            
            # Запускаем потоки
            caps_thread = threading.Thread(target=self.watch_caps_lock, daemon=True)
            caps_thread.start()
            
            # Основной цикл
            self.zalgo_loop()
            
        except KeyboardInterrupt:
            print("\nDemon Zalgo остановлен пользователем")
            self.log("Demon Zalgo остановлен пользователем")
        except Exception as e:
            print(f"\nКритическая ошибка: {e}")
            self.log(f"Критическая ошибка: {e}")
        finally:
            self.running = False
            print("Demon Zalgo завершил работу")

def main():
    """Точка входа"""
    try:
        demon = ZalgoDemon()
        demon.run()
    except Exception as e:
        print(f"Ошибка запуска: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
