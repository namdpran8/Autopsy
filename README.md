# 🔍 Autopsy — AutoDebug Agent

> AI-powered debugging agent that analyzes GitLab CI/CD pipeline failures and generates fix suggestions using Google Gemini.

---

## 📋 Table of Contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Usage](#usage)
- [Docker](#docker)
- [API Reference](#api-reference)
- [License](#license)

---

## ✨ Features

- 🔗 Connects to your GitLab project and monitors pipeline failures
- 🤖 Sends failure logs to Google Gemini for root-cause analysis
- 🩹 Returns actionable fix suggestions via a REST API
- 🐳 Ships as a lightweight Docker container

---

## ⚙️ Prerequisites

- Python 3.11+
- A [Google Gemini API key](https://aistudio.google.com/apikey)
- A GitLab personal access token with **api** scope

---

## 🚀 Setup

### 1. Clone the repository

```bash
git clone https://github.com/your-org/autopsy.git
cd autopsy
```

### 2. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate   # Linux / macOS
.venv\Scripts\activate      # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in your values:

| Variable             | Description                              |
|----------------------|------------------------------------------|
| `GEMINI_API_KEY`     | Your Google Gemini API key               |
| `GITLAB_TOKEN`       | GitLab personal access token (api scope) |
| `GITLAB_PROJECT_ID`  | Numeric project ID from GitLab           |
| `GITLAB_URL`         | GitLab instance URL (default: gitlab.com)|

---

## 🏃 Usage

### Start the server locally

```bash
uvicorn api.main:app --reload --port 8080
```

### Example: analyze a failed pipeline

```bash
curl -X POST http://localhost:8080/analyze \
  -H "Content-Type: application/json" \
  -d '{"pipeline_id": 123456}'
```

**Response:**

```json
{
  "pipeline_id": 123456,
  "status": "failed",
  "root_cause": "ModuleNotFoundError: No module named 'requests'",
  "suggestion": "Add 'requests' to requirements.txt and rebuild the CI image."
}
```

---

## 🐳 Docker

### Build the image

```bash
docker build -t autopsy .
```

### Run the container

```bash
docker run -d \
  --name autopsy \
  --env-file .env \
  -p 8080:8080 \
  autopsy
```

---

## 📡 API Reference

| Method | Endpoint     | Description                        |
|--------|--------------|------------------------------------|
| `GET`  | `/health`    | Health check                       |
| `POST` | `/analyze`   | Analyze a failed pipeline by ID    |

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).

© 2026 AutoDebug Contributors
