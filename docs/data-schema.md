# Схема данных

Схема создаётся в `init_db()` (`app.py`) одним `executescript` и состоит из семи
таблиц SQLite. Все денежные суммы хранятся целыми числами в минимальных
единицах валюты (копейки/центы).

## ER-диаграмма

```mermaid
erDiagram
    budget_lines ||--o{ budget_operations : "источник (source_budget_id)"
    budget_lines ||--o{ budget_operations : "получатель (target_budget_id)"
    budget_lines ||--o{ purchase_orders : "заказы"
    budget_lines ||--o{ expenses : "расходы"
    budget_lines ||--o{ budget_monthly_allocations : "план по месяцам"
    purchase_orders ||--o{ expenses : "списания по заказу"

    budget_lines {
        INTEGER id PK
        TEXT code UK "уникальный код линии"
        TEXT name
        INTEGER fiscal_year
        TEXT holder_name
        TEXT holder_email "nullable"
        TEXT cost_center "nullable"
        TEXT wbs "nullable"
        TEXT cost_element "nullable"
        TEXT currency "DEFAULT EUR"
        INTEGER initial_approved_cents ">= 0"
        INTEGER initial_released_cents ">= 0"
        TEXT created_at
    }

    budget_operations {
        INTEGER id PK
        TEXT operation_type "SUPPLEMENT REDUCTION RELEASE RETURN TRANSFER CARRY_FORWARD"
        INTEGER source_budget_id FK "nullable"
        INTEGER target_budget_id FK "nullable"
        INTEGER amount_cents "> 0"
        INTEGER approved_delta_source
        INTEGER released_delta_source
        INTEGER approved_delta_target
        INTEGER released_delta_target
        TEXT note "nullable"
        TEXT created_by
        TEXT created_at
    }

    purchase_orders {
        INTEGER id PK
        TEXT number UK "уникальный номер заказа"
        INTEGER budget_id FK "NOT NULL"
        TEXT vendor
        TEXT description
        INTEGER amount_cents "> 0"
        TEXT status "DRAFT APPROVED CLOSED CANCELLED"
        TEXT created_at
    }

    expenses {
        INTEGER id PK
        INTEGER budget_id FK "NOT NULL"
        INTEGER po_id FK "nullable, расход без заказа"
        TEXT expense_date
        TEXT invoice_no "nullable"
        TEXT description
        INTEGER amount_cents "> 0"
        TEXT created_at
    }

    budget_monthly_allocations {
        INTEGER budget_id PK "FK, часть составного ключа"
        INTEGER month PK "1..12"
        INTEGER allocated_cents ">= 0"
    }

    currencies {
        TEXT code PK "ISO-код"
        TEXT name
        INTEGER rate_micro "курс к RUB x1e6, nullable"
        INTEGER is_active "0 или 1"
        TEXT updated_at "nullable"
    }

    app_settings {
        TEXT key PK
        TEXT value
    }
```

`currencies` и `app_settings` связаны с остальными таблицами только логически:
`budget_lines.currency` хранит ISO-код без внешнего ключа, а настройки — это
key-value с известными ключами `base_currency` и `rates_updated_at`.

## Назначение таблиц

| Таблица | Назначение |
| --- | --- |
| `budget_lines` | Бюджетная линия. Хранит только начальные суммы; текущие считаются на лету |
| `budget_operations` | Журнал движений бюджета с дельтами отдельно для источника и получателя |
| `purchase_orders` | Заказы поставщикам; обязательства формируют только со статусом `APPROVED` |
| `expenses` | Фактические расходы, с привязкой к заказу или без неё |
| `budget_monthly_allocations` | Помесячный план по линии, составной ключ `(budget_id, month)` |
| `currencies` | Справочник валют и курсы ЦБ РФ к рублю |
| `app_settings` | Key-value настройки приложения |

## Что считается, а не хранится

Текущее состояние бюджета в таблицах отсутствует — `budget_metrics()` собирает
его при каждом запросе:

| Показатель | Формула |
| --- | --- |
| `approved` | `initial_approved_cents` + Σ `approved_delta` (source + target) |
| `released` | `initial_released_cents` + Σ `released_delta` (source + target) |
| `actuals` | Σ `expenses.amount_cents` |
| `commitments` | Σ `max(po.amount_cents − Σ расходов по заказу, 0)` для `status = 'APPROVED'` |
| `available` | `released − actuals − commitments` |

## Как операции меняют суммы

`compute_operation_deltas()` превращает тип операции и сумму в четыре дельты,
которые записываются в `budget_operations`. Знак указан относительно суммы.

| Операция | Утв. источника | Выд. источника | Утв. получателя | Выд. получателя | Проверка |
| --- | --- | --- | --- | --- | --- |
| `SUPPLEMENT` | +сумма | +сумма | 0 | 0 | Дополнительное финансирование |
| `REDUCTION` | −сумма | −сумма | 0 | 0 | Остаток не ниже факта и обязательств |
| `RELEASE` | 0 | +сумма | 0 | 0 | Выделено не выше утверждённого |
| `RETURN` | 0 | −сумма | 0 | 0 | Нельзя вернуть израсходованное |
| `TRANSFER` | −сумма | −сумма | +сумма | +сумма | Одна валюта, достаточный остаток |
| `CARRY_FORWARD` | −сумма | −сумма | +сумма | +сумма | То же плюс `fiscal_year` получателя строго больше |

## Инварианты и хранение

- Деньги — целые числа в минимальных единицах; дробных типов в схеме нет.
- `assert_budget_ok()` после каждой записи проверяет `released <= approved` и
  `available >= 0`, иначе транзакция откатывается.
- Записи идут через `BEGIN IMMEDIATE`, поэтому проверка остатка и запись
  атомарны относительно других писателей.
- `PRAGMA foreign_keys = ON` включается на каждом соединении.
- Курсы валют хранятся к рублю целыми числами ×1 000 000, RUB зафиксирован
  как 1 000 000.
- Индексов, представлений и миграций нет: `init_db()` создаёт таблицы через
  `CREATE TABLE IF NOT EXISTS`.
