# Деплой translator

## Окружения

| Среда | Ветка  | URL                           | Coolify UUID             |
|-------|--------|-------------------------------|--------------------------|
| prod  | master | https://translator.luna.lc    | by7ldnzgf9qtlqjhdie0li8f |

## Coolify проект

- **Project UUID**: `g9u99o3jggkwcz4c0uhv4h25`
- **Environment UUID**: `i123ozlbyct2cj5zzvvh2s3h` (production)
- **Server**: `rfxhu806eptj42yt1uzsdlk6` (luna-main)

## Триггер деплоя

- Ручной: `coolify deploy uuid by7ldnzgf9qtlqjhdie0li8f`
- Auto-webhook: не настроен (настроить в Coolify UI при необходимости)

## Обязательные env-переменные

Нет (конфигурация захардкожена в коде).

## Зависимости

Нет (stateless, PDF обрабатывается в памяти).

## Грабли проекта

- Ветка `master`, не `main` — не перепутать при триггере деплоя
- Публичный репо `igor-lwam/translator` (не `lwamltd`) — использовать `applications/public`, не `private-github-app`
- Security headers добавлены через `custom_labels` (Traefik middleware `translator-security`), не через код
- `server: uvicorn` в ответах — Traefik не скрывает этот заголовок (только Caddy в `caddy_0.header=-Server`), но Coolify работает на Traefik — не критично
- CORS: `allow_origins=["*"]` в коде — намеренно, т.к. нет auth/cookies, инструмент внутренний
- Контейнер stateless — нет volumes (PDF обрабатывается в памяти, данные не персистируются)
