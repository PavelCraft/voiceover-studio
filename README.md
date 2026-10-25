# Voiceover Studio

## Русский

Локальный сервис для подготовки озвучки видео: загрузки аудио и видео, нарезки речи по таймкодам, поиска пауз, подгонки скорости отдельных фраз и экспорта готового видео или отдельной аудиодорожки для YouTube.

### Установка

Клонируйте репозиторий и перейдите в его папку:

```bash
git clone git@github.com:PavelCraft/voiceover-studio.git
cd voiceover-studio
```

Создайте виртуальное окружение Python 3.12 и активируйте его.

Linux:

```bash
python3.12 -m venv venv
source venv/bin/activate
```

macOS:

```bash
python3.12 -m venv venv
source venv/bin/activate
```

Windows (Git Bash):

```bash
python3.12 -m venv venv
source venv/Scripts/activate
```

Установите зависимости:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Скопируйте шаблон переменных окружения:

```bash
cp .env.example .env
```

При необходимости укажите в `.env` ключ Groq:

```env
GROQ_API_KEY=ваш_ключ_groq
```

Не добавляйте `.env` в Git и не публикуйте API-ключ.

### Подготовка базы данных и запуск

```bash
python manage.py migrate
python manage.py runserver
```

Откройте в браузере:

```text
http://127.0.0.1:8000/
```

### Использование

Загрузите озвучку и сценарий в TXT, SRT, VTT или CSV, затем исходное видео и настройки экспорта. Долговечная очередь продолжает обработку независимо от открытой страницы. После проверки доступны M4A/WAV, отдельные фрагменты, SRT, CSV и FCPXML для Premiere/DaVinci. Инструмент «Аудио в текст» распознаёт речь, сохраняет пунктуацию и абзацы и экспортирует редактируемый результат в TXT или DOCX. Для работы требуются FFmpeg и действующий `GROQ_API_KEY` в `.env`.

### Очередь и хранение

Локально `VOICEOVER_AUTOSTART_WORKER=1` запускает одноразовый обработчик. На сервере установите `VOICEOVER_AUTOSTART_WORKER=0` и держите отдельный процесс:

```bash
python manage.py run_voiceover_worker
```

Зависшие задания возвращаются в очередь, неудачные можно повторить, активные — отменить. Ограничения длительности и месячные квоты сейчас отключены. Если лимиты появятся позже, они должны проверяться до принятия видео или аудио; уже запущенное задание должно всегда получить возможность завершиться.

Политику хранения пользователь выбирает в проекте. Worker проверяет сроки при запуске и затем раз в сутки; ручная или cron-проверка доступна командой:

```bash
python manage.py cleanup_expired_projects
```

### Production-деплой

Рекомендуемый адрес приложения — `https://studio.formacode.dev`. Next.js-сайт
`formacode.dev` и Django-студия запускаются независимо, а Nginx направляет
поддомен студии в Gunicorn на `127.0.0.1:8001`.

Первичная установка выполняется один раз:

```bash
sudo git clone <repository-url> /var/www/voiceover-studio
sudo chown -R deploy:deploy /var/www/voiceover-studio
cd /var/www/voiceover-studio
sudo bash deploy/bootstrap-server.sh
sudo nano /etc/voiceover-studio.env
```

После заполнения `.env`:

```bash
sudo -u deploy bash -lc 'set -a; source /etc/voiceover-studio.env; cd /var/www/voiceover-studio; .venv/bin/python manage.py migrate --noinput; .venv/bin/python manage.py compilemessages -l ru; .venv/bin/python manage.py collectstatic --noinput'
sudo systemctl enable --now voiceover-studio-web voiceover-studio-worker
sudo systemctl reload nginx
```

Шаблоны находятся в `deploy/`: два systemd unit, Nginx-конфигурация и
production-переменные. Bootstrap создаёт отдельную loopback-файловую систему
`media` размером 7 ГБ. Программный лимит составляет 6 ГБ, а новая операция
принимается только если после зарезервированного объёма на корневом разделе
останется не менее 8 ГБ.

После первой установки обычное обновление запускается локально:

```bash
make deploy
```

Команда проверяет чистоту серверного репозитория, применяет только fast-forward,
делает резервную копию SQLite, устанавливает зависимости, выполняет проверки,
миграции, компиляцию переводов, `collectstatic` и перезапускает web и worker.
Перед `make deploy` изменения должны быть отправлены в `origin/main`.

### Архитектура

- `voiceover_core` — независимые парсеры, сопоставление, диагностика, FFmpeg-фильтры и экспорт;
- `voiceover_jobs` — контракты долговечных заданий;
- `voiceover_projects` — структуры проекта, настройки экспорта и интерфейс биллинга;
- `studio` — Django-формы, ORM, HTTP и адаптеры Groq/FFmpeg.

`voiceover_core` не импортирует Django, модели или `settings.MEDIA_ROOT`, поэтому его можно использовать в будущем Electron-приложении без веб-сервера.

---

## English

Local service for preparing video voiceovers: uploading audio and video, splitting speech by timecodes, detecting pauses, adjusting the speed of individual phrases, and exporting either a finished video or a separate audio track for YouTube.

### Installation

Clone the repository and enter its directory:

```bash
git clone git@github.com:PavelCraft/voiceover-studio.git
cd voiceover-studio
```

Create and activate a Python 3.12 virtual environment.

Linux:

```bash
python3.12 -m venv venv
source venv/bin/activate
```

macOS:

```bash
python3.12 -m venv venv
source venv/bin/activate
```

Windows (Git Bash):

```bash
python3.12 -m venv venv
source venv/Scripts/activate
```

Install the dependencies:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Copy the environment template:

```bash
cp .env.example .env
```

If needed, add a Groq API key to `.env`:

```env
GROQ_API_KEY=your_groq_api_key
```

Never commit `.env` or publish an API key.

### Database setup and local server

```bash
python manage.py migrate
python manage.py runserver
```

Open this address in your browser:

```text
http://127.0.0.1:8000/
```

### Usage

Upload the voiceover and a TXT, SRT, VTT or CSV script, then add the source video and export settings. The durable queue runs independently from the page. Exports include M4A/WAV, separate clips, SRT, CSV and FCPXML for Premiere/DaVinci. The Audio to text tool recognizes speech, formats punctuation and paragraphs, and exports editable TXT or DOCX. See the Russian section for worker, retention and architecture commands. FFmpeg and a valid `GROQ_API_KEY` are required.
