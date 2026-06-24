# TenderMate Backend (FastAPI)

Бэкенд разбора тендеров для Timeweb App Platform.

## Переменные окружения (в Timeweb → настройки приложения)

| Переменная | Значение |
|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-...` с console.anthropic.com |
| `SUPABASE_URL` | `https://xxx.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | service_role ключ из Supabase → Settings → API |
| `DADATA_TOKEN` | токен DaData (необязателен, для автозаполнения по ИНН) |

## Команды для Timeweb App Platform

- Команда сборки: `pip install --upgrade -r requirements.txt`
- Команда запуска: `uvicorn main:app --host 0.0.0.0 --port 80`
- Путь проверки состояния: `/health`

## Эндпоинты

- `GET /health` — проверка
- `POST /analyze` — создаёт задачу разбора, возвращает `{job_id}`
- `GET /analyze?id={job_id}` — статус и результат
- `POST /analyze` с `{"action":"lookup","inn":"..."}` — данные компании по ИНН
