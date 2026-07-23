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
- полный CRUDL (Create, Read, Update, Delete, List) для всех сущностей: бюджетов, PO, расходов и операций;
- мультивалютность: справочник валют, активные валюты, курсы ЦБ РФ по запросу, основная валюта отображения и переключатель валюты на каждой странице;
- двуязычный интерфейс (русский/английский) с переключателем языка;
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

## CRUDL и маршруты

Для каждой сущности доступны все операции CRUDL:

| Сущность  | Create        | Read (карточка)   | Update            | Delete              | List          |
|-----------|---------------|-------------------|-------------------|---------------------|---------------|
| Бюджеты   | `/budgets/new`| `/budgets/{id}`   | `/budgets/{id}/edit` | `/budgets/{id}/delete` | `/budgets`    |
| PO        | `/pos/new`    | `/pos/{id}`       | `/pos/{id}/edit` (+ `/pos/{id}/status`) | `/pos/{id}/delete` | `/pos`        |
| Расходы   | `/expenses/new` | `/expenses/{id}` | `/expenses/{id}/edit` | `/expenses/{id}/delete` | `/expenses`   |
| Операции  | `/budgets/{id}/operation` | `/operations/{id}` | `/operations/{id}/edit` | `/operations/{id}/delete` | `/operations` |

Правила целостности при изменении и удалении:

- любое изменение или удаление проверяется на инварианты бюджета
  (`Available ≥ 0` и `Released ≤ Approved`); нарушающая операция откатывается;
- бюджет нельзя удалить, пока с ним связаны PO, расходы или операции;
- PO нельзя удалить, пока по нему проведены расходы; редактируются только
  Draft и Approved PO, а сумма PO не может быть меньше уже проведённых расходов;
- удаление или редактирование операции пересчитывает дельты и заново проверяет
  затронутые бюджеты (например, удаление `SUPPLEMENT`, средства которого уже
  израсходованы, будет отклонено).

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
- `DEFAULT_LANG` — язык интерфейса по умолчанию (`en` или `ru`, по умолчанию `en`);
- `CBR_URL` — источник курсов ЦБ РФ (по умолчанию `https://www.cbr.ru/scripts/XML_daily.asp`);
- `APP_USER` и `APP_PASSWORD` — включить HTTP Basic Authentication.

## Локализация (i18n)

Интерфейс доступен на русском и английском языках. Язык выбирается в следующем
порядке приоритета:

1. параметр запроса `?lang=ru` или `?lang=en` (выбор запоминается в cookie `lang`);
2. cookie `lang`;
3. заголовок браузера `Accept-Language`;
4. значение `DEFAULT_LANG` (по умолчанию `en`).

В шапке страницы есть переключатель `RU/EN`. Все строки интерфейса собраны в
одном каталоге `TRANSLATIONS` в [app.py](app.py); русские строки снабжены
английскими комментариями. Суммы форматируются по локали: `1,234.56 EUR` для
английского и `1 234,56 EUR` (с неразрывным пробелом) для русского.

<!-- English: the UI ships in Russian and English. The language is resolved
     from ?lang=, then the `lang` cookie, then Accept-Language, then
     DEFAULT_LANG. Every user-visible string lives in the TRANSLATIONS catalog
     in app.py, whose Russian entries carry English comments. -->

## Валюты и курсы ЦБ РФ

Приложение поддерживает несколько валют. Справочник и настройки — на странице
`/settings`:

- **Активные валюты** отмечаются флажками; только активные доступны для выбора
  при создании и редактировании бюджета.
- **Основная валюта** (по умолчанию `RUB`) — валюта отображения по умолчанию, в
  которой показываются все документы и итоги на дашборде; её нельзя
  деактивировать.
- **Курсы ЦБ РФ** загружаются по кнопке «Обновить курсы» (запрос к ЦБ РФ,
  XML в кодировке windows-1251). Курсы кэшируются в таблице `currencies` и не
  запрашиваются при старте; при недоступности сети показывается ошибка, а старые
  курсы сохраняются. Все курсы хранятся относительно рубля.

Валюта выбирается **на уровне бюджета**; PO и расходы наследуют её. Пересчёт по
курсу используется только для отображения: суммы хранятся в исходной валюте
бюджета, поэтому математика бюджета (`Available`, инварианты) остаётся точной. На
каждой странице есть переключатель валюты отображения (`?ccy=USD`); если она
отличается от исходной, рядом показывается исходная сумма, а при отсутствии курса
— исходная сумма с пометкой.

Ограничения: переносы (`TRANSFER`) между разными валютами не поддерживаются;
история курсов не хранится (используется последний загруженный курс); все валюты
считаются двузначными.

<!-- English: multi-currency support. Currencies live in the `currencies` table;
     some are marked active and become selectable per budget. Rates are fetched
     from the CBR on demand (windows-1251 XML), cached, and stored relative to
     RUB. A budget carries the currency; POs/expenses inherit it and amounts are
     stored in that currency, so budget math stays exact. Conversion is
     display-only: one base display currency (default RUB) plus a per-page
     ?ccy= switch. Cross-currency transfers are unsupported; the latest rate is
     used (no history); all currencies are treated as 2-decimal. -->

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

Тесты используют по-умолчанию используют стандартную библиотеку:

```bash
python3 -m unittest -v
```

Покрыты разбор сумм, метрики бюджета (`Available`, `Commitments`), правила
операций, инварианты бюджета при изменении/удалении (`assert_budget_ok`) и
защита от гонки: параллельные записи, изменяющие бюджет, выполняются в
транзакции `BEGIN IMMEDIATE`, поэтому проверка доступного остатка и запись
атомарны и бюджет нельзя перерасходовать.

Также покрыты парсинг курсов ЦБ РФ (`parse_cbr_rates`), конвертация валют
(`convert_cents`), обновление курсов (`refresh_rates` с подменой сетевого
запроса) и настройки (`get_setting`/`set_setting`) — всё без обращения к сети.
