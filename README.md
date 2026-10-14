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

Загрузите озвучку, вставьте текст с таймкодами или приложите TXT-файл, затем загрузите исходное видео и выберите режим экспорта. Сервис распознает слова через Groq, сопоставит их с фразами, найдёт реальные паузы, нарежет аудио, ускорит слишком длинные фрагменты и соберёт готовое видео или аудиодорожку. Обработка выполняется в фоне, а текущие этапы отображаются на странице проекта, поэтому страницу можно покинуть и открыть позднее. После обработки каждый фрагмент можно прослушать в браузере. Для работы требуются FFmpeg и действующий `GROQ_API_KEY` в `.env`.

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

Upload the voiceover, paste the timecoded text or attach a TXT file, then upload the source video and choose the export mode. The service recognizes words with Groq, aligns them with the phrases, detects real pauses, cuts the audio, speeds up fragments that do not fit, and renders a finished video or audio track. Processing runs in the background and the project page shows each current stage, so you can leave it and return later. Every fragment can be reviewed in the browser after processing. FFmpeg and a valid `GROQ_API_KEY` in `.env` are required.
