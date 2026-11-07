import os
import time
import subprocess
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Папка, за которой ведётся слежение
WATCHED_FOLDER = r'c:\Users\DoOs\R_tail04\Bayki_tetuhki_Simurgh'
# Имя скрипта для слияния файлов
SCRIPT = 'merge_cross_files.py'
# Файлы, которые не должны вызывать слияние при изменении
EXCLUDE_FILES = ['result.md', 'README.md', 'туду.md']

class ChangeHandler(FileSystemEventHandler):
    # Обработчик события изменения файла
    def on_modified(self, event):
        filename = os.path.basename(event.src_path)
        # Проверяем, что изменённый файл — это .md и не из списка исключённых
        if filename.endswith('.md') and filename not in EXCLUDE_FILES:
            print(f"[{time.strftime('%H:%M:%S')}] Изменён файл: {event.src_path}")
            print(f"[{time.strftime('%H:%M:%S')}] Запуск слияния...")
            # Запускаем скрипт слияния и выводим его результат
            result = subprocess.run(['py', SCRIPT], cwd=WATCHED_FOLDER, capture_output=True, text=True)
            print(f"[{time.strftime('%H:%M:%S')}] merge_cross_files.py STDOUT:\n{result.stdout}")
            if result.stderr:
                print(f"[{time.strftime('%H:%M:%S')}] merge_cross_files.py STDERR:\n{result.stderr}")

if __name__ == "__main__":
    # Запуск наблюдателя за изменениями
    print("Слежение за изменениями запущено. Для выхода нажмите Ctrl+C.")
    print(f"Папка для отслеживания: {WATCHED_FOLDER}")
    event_handler = ChangeHandler()
    observer = Observer()
    observer.schedule(event_handler, WATCHED_FOLDER, recursive=False)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()