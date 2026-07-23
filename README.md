# Budget Control

Контейнеризированное MVP-приложение для управления бюджетами, PO, commitments и actuals.

## Возможности

- бюджеты по финансовому году, Budget Holder, Cost Center, WBS и Cost Element;
- approved и released budget;
- операции Supplement, Reduction, Release, Return, Transfer и Carry Forward;
- PO в статусах Draft, Approved, Closed и Cancelled;
- резервирование бюджета через открытые Approved PO;
- фактические расходы с привязкой к PO или без PO;
- автоматическое преобразование commitment в actual при внесении расхода по PO;
- освобождение остатка бюджета при закрытии PO;
- журнал операций и JSON API `/api/summary`;
- SQLite в Docker volume;
- опциональная HTTP Basic Authentication.

## Запуск

```bash
docker compose up -d --build
```

Открыть: `http://localhost:8080`

Проверка:

```bash
curl http://localhost:8080/healthz
curl http://localhost:8080/api/summary
```

## Основная логика

```text
Available = Released - Actuals - Commitments
Commitment PO = PO amount - expenses linked to this PO
```

- Draft PO не резервирует бюджет.
- Approved PO резервирует неиспользованный остаток PO.
- Расход по PO увеличивает actuals и на ту же сумму уменьшает commitment.
- Closed/Cancelled PO не создаёт commitment; остаток освобождается.
- Расход без PO непосредственно уменьшает available budget.

## Операции бюджета

- `SUPPLEMENT`: увеличивает approved и released.
- `REDUCTION`: уменьшает approved и released; запрещена, если затрагивает уже использованный бюджет.
- `RELEASE`: увеличивает released в пределах approved.
- `RETURN`: уменьшает released, но не ниже actuals + commitments.
- `TRANSFER`: переносит approved и released между бюджетами одной валюты.
- `CARRY_FORWARD`: переносит бюджет в бюджет более позднего финансового года.

## Данные и резервное копирование

База находится в volume `budget_data`, файл `/data/budget.db`.

Резервная копия:

```bash
docker compose exec budget-manager python -c "import sqlite3; s=sqlite3.connect('/data/budget.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close(); s.close()"
docker cp budget-manager:/data/backup.db ./backup.db
```

## Настройки

Переменные окружения:

- `PORT` — порт внутри контейнера, по умолчанию `8080`;
- `DB_PATH` — путь к SQLite;
- `SEED_DEMO=1` — создать демонстрационные записи в пустой БД;
- `APP_USER` и `APP_PASSWORD` — включить HTTP Basic Authentication.

## Ограничения MVP

- нет полноценной модели пользователей и разграничения ролей;
- нет согласования PR/PO по нескольким уровням;
- нет вложений, импорта/экспорта Excel и интеграции с ERP;
- нет налогового и бухгалтерского журнала проводок;
- SQLite подходит для небольшой внутренней команды, но не для высокой конкурентной нагрузки;
- для публикации наружу нужен HTTPS reverse proxy и управление секретами.

## Запуск без Docker

Внешние Python-библиотеки не требуются:

```bash
mkdir -p data
DB_PATH=./data/budget.db PORT=8080 SEED_DEMO=1 python3 app.py
```

## Тесты

Тесты используют только стандартную библиотеку:

```bash
python3 -m unittest -v
```

Покрыты разбор сумм, метрики бюджета (`Available`, `Commitments`), правила
операций и защита от гонки: параллельные записи, изменяющие бюджет,
выполняются в транзакции `BEGIN IMMEDIATE`, поэтому проверка доступного
остатка и запись атомарны и бюджет нельзя перерасходовать.
