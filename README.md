# Budget Control

Контейнеризированное MVP-приложение для управления бюджетами, PO, commitments и actuals.

## Возможности

- бюджеты по финансовому году, Budget Holder, Cost Center, WBS и Cost Element —
  все они выбираются из справочников, а не вводятся текстом;
- справочники Function, Sub-function, Project, WBS, Cost Center, Cost Element,
  Budget Holder, Vendor и валюты с полным CRUDL;
- структурный WBS: `Function(2) / Sub-function(4) / Project.extension`,
  уникальный, с обязательным бюджетом на каждый элемент;
- все денежные значения — `decimal` с двумя знаками (полей «в центах» нет);
- approved и released budget;
- помесячный план бюджета: распределение по 12 месяцам финансового года,
  факт и остаток по каждому месяцу, мягкий контроль превышения;
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

Порт публикуется только на петлевом интерфейсе (`127.0.0.1:8080`). Чтобы
открыть доступ извне хоста, поставьте перед приложением reverse proxy с TLS:
Basic Authentication передаёт пароль в открытом виде.

Проверка:

```bash
curl http://localhost:8080/healthz
curl http://localhost:8080/api/summary
```

`/healthz` намеренно отвечает без аутентификации — его опрашивает `HEALTHCHECK`
контейнера, и за Basic Auth контейнер уходил бы в статус `unhealthy`. Эндпоинт
не отдаёт ничего, кроме признака доступности. Все остальные маршруты, включая
`/api/summary`, закрыты, если заданы `APP_USER` и `APP_PASSWORD`.

## Справочники и WBS

Каждая сущность ведётся в справочнике (`/references`), документы ссылаются на
записи по id:

| Справочник | Страница | Правило кода |
|---|---|---|
| Function | `/references/functions` | ровно 2 знака (`A–Z`, `0–9`) |
| Sub-function | `/references/sub-functions` | ровно 4 знака, принадлежит функции |
| Project | `/references/projects` | до 15 знаков (`A–Z`, `0–9`, дефис) |
| Cost Center | `/references/cost-centers` | `A–Z`, `0–9`, дефис |
| Cost Element | `/references/cost-elements` | `A–Z`, `0–9`, дефис |
| Budget Holder | `/references/holders` | код + имя + email |
| Vendor | `/references/vendors` | код + название |
| WBS-элементы | `/wbs` | собирается из уровней, см. ниже |
| Валюты | `/settings` | трёхбуквенный код + курс ЦБ РФ |

Запись справочника нельзя удалить, пока на неё кто-то ссылается.

### Формирование WBS

```text
<префикс><Function>/<Sub-function>/<Project>.<extension>
             2 знака      4 знака    3-й и 4-й уровни: не более 15 знаков вместе
```

- полный WBS начинается с кода-префикса (`/settings` → «Кодирование WBS»);
  по умолчанию префикс пустой, и WBS начинается прямо с кода функции;
- при изменении префикса или кода любого уровня коды всех WBS-элементов
  пересобираются автоматически;
- WBS **уникален**: одному WBS-элементу соответствует ровно одна бюджетная
  строка (`budget_lines.wbs_element_id` — `NOT NULL UNIQUE`);
- **бюджет должен быть создан для каждого** WBS-элемента: на страницах `/wbs` и
  `/budgets` выводится число элементов без бюджета, а в списке WBS у каждого
  такого элемента есть кнопка «Создать бюджет» (`/budgets?wbs={id}`);
- субфункция должна принадлежать выбранной функции, иначе элемент не сохранится.

<!-- English: every entity is kept in a reference catalog under /references and
     documents point at catalog entries by id. A WBS element is assembled from
     Function (2 chars) / Sub-function (4 chars) / Project.extension, where the
     project and extension together may not exceed 15 characters. The full WBS
     starts with the prefix code configured on /settings (empty by default).
     Changing the prefix or any level code rebuilds every stored WBS code. A WBS
     is unique and carries exactly one budget line, and both /wbs and /budgets
     report the elements that still need a budget. -->

## Денежные суммы

Все суммы — `Decimal` с двумя знаками после запятой, и в приложении, и в базе:
колонки объявлены `NUMERIC(18,2)` (курсы — `NUMERIC(18,6)`), отдельных полей с
минимальными единицами (`*_cents`, `rate_micro`) больше нет.

- ввод разбирается `parse_money()` и принимает `1 234,56` и `1,234.56`;
- чтение из SQLite нормализует значение `dec()` — SQLite хранит `NUMERIC` как
  INTEGER или REAL, поэтому значение всегда приводится обратно к двум знакам;
- суммирование в SQL идёт через собственный агрегат `dsum()`, который
  складывает `Decimal`, а не REAL, — итоги точны до копейки;
- `/api/summary` отдаёт суммы строками (`"available": "48765.94"`), потому что
  JSON-число — это двоичный float.

База, созданная предыдущей версией, **мигрирует при старте**: суммы делятся на
100, `rate_micro` — на 1 000 000, а текстовые Budget Holder, Vendor, Cost
Center, Cost Element и WBS переносятся в справочники. WBS в стандартном формате
разбирается на уровни; нераспознанный текст попадает в запасной элемент
`ZZ/ZZZZ/<старый текст>`, который затем можно переоформить вручную.

<!-- English: all amounts are two-decimal Decimals, stored in NUMERIC(18,2)
     columns; no minor-unit integer fields remain. dec() normalises values read
     back from SQLite and the custom dsum() aggregate keeps SQL summation exact.
     /api/summary returns amounts as strings. A database from the previous
     version is migrated on start: amounts are divided by 100, rates by 1e6, and
     the free-text fields are folded into the new catalogs. -->

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

## Помесячный план

На карточке бюджета (`/budgets/{id}`) можно распределить бюджет по 12 месяцам
финансового года (форма сохраняется в `POST /budgets/{id}/allocations`; кнопка
«Распределить равномерно» делит released-бюджет на 12 частей). Расходы
относятся к месяцу по своей дате; по каждому месяцу показываются план, факт,
остаток и статус.

Контроль помесячного плана — **мягкий**: расход сверх плана месяца проводится,
но месяц подсвечивается статусом «Превышение», а после проведения показывается
предупреждение. Жёстким остаётся только годовой контроль (`Available ≥ 0`).
Если план не задан, бюджет ведёт себя как раньше — только годовой контроль.
Список расходов (`/expenses`) поддерживает фильтр по месяцу (`?month=YYYY-MM`).

<!-- English: monthly plan. A budget line's released budget can be allocated
     across the 12 months of its fiscal year on the budget detail page
     (POST /budgets/{id}/allocations; a "distribute evenly" button splits the
     released amount). Expenses are bucketed by expense date; each month shows
     plan, actuals, remaining and status. Monthly control is soft: an over-plan
     expense is still posted but the month is flagged and a warning flash is
     shown. Only the annual `Available >= 0` check blocks postings. Lines
     without a plan behave as before. The expenses list accepts a
     ?month=YYYY-MM filter. -->


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
| Справочники | `/references/{slug}/new` | `/references/{slug}/{id}` | `/references/{slug}/{id}/edit` | `/references/{slug}/{id}/delete` | `/references`, `/references/{slug}` |
| WBS       | `/wbs/new`    | `/wbs/{id}`       | `/wbs/{id}/edit`  | `/wbs/{id}/delete`  | `/wbs`        |

Помесячный план бюджета сохраняется через `POST /budgets/{id}/allocations`
(12 сумм; пустые поля означают 0, полностью пустая форма удаляет план).

Правила целостности при изменении и удалении:

- любое изменение или удаление проверяется на инварианты бюджета
  (`Available ≥ 0` и `Released ≤ Approved`); нарушающая операция откатывается;
- бюджет нельзя удалить, пока с ним связаны PO, расходы или операции;
- запись справочника нельзя удалить, пока на неё ссылаются документы или
  WBS-элементы; WBS-элемент нельзя удалить, пока по нему заведён бюджет;
- WBS уникален, и повторное использование занятого WBS-элемента отклоняется;
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

Конфигурации в образе нет: всё читается из переменных окружения при старте.
Полный список с комментариями и значениями по умолчанию — в `.env.example`;
скопируйте его в `.env`, и `docker compose` подхватит файл автоматически.
Пустое значение переменной считается незаданным, и применяется значение по
умолчанию, — так платформы, подставляющие пустые строки, ничего не ломают.

### Приложение

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `APP_NAME` | `Budget Control` | Название в интерфейсе |
| `DATA_DIR` | `/data` | Каталог со всеми персистентными файлами. Монтировать нужно именно его |
| `DB_PATH` | `$DATA_DIR/budget.db` | Путь к SQLite, если БД лежит вне `DATA_DIR` |
| `DB_TIMEOUT` | `30` | Сколько секунд ждать разблокировки БД |
| `DATA_FILE_MODE` | `0660` | Права на файлы БД при старте |
| `HOST` | `0.0.0.0` | Интерфейс внутри контейнера |
| `PORT` | `8080` | Порт внутри контейнера; хостинг может подставить свой |
| `SEED_DEMO` | `1` | Демо-записи в пустой БД. Для боевого стенда `0` |
| `DEFAULT_LANG` | `en` | Язык интерфейса: `en` или `ru` |
| `APP_USER`, `APP_PASSWORD` | пусто | Включают HTTP Basic Authentication |
| `COOKIE_SECURE` | `auto` | `auto` ставит `Secure`, только если запрос пришёл по HTTPS |
| `FORWARDED_PROTO_HEADER` | `X-Forwarded-Proto` | Заголовок, по которому прокси сообщает схему. Пустое значение выключает проверку |
| `CBR_URL` | endpoint ЦБ РФ | Источник курсов валют |
| `CBR_TIMEOUT` | `10` | Таймаут запроса курсов, секунды |

Булевы переменные принимают `1/0`, `true/false`, `yes/no`, `on/off`.
Некорректное значение — не молчаливый фолбэк, а остановка на старте с
сообщением, какая переменная и что именно с ней не так.

### Запуск контейнера

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `RUN_UID`, `RUN_GID` | `10001`, `0` | Учётная запись, под которой работает приложение |
| `UMASK` | `0002` | Маска для новых файлов |
| `CHOWN_DATA_DIR` | `1` | Передавать ли `DATA_DIR` во владение `RUN_UID`, если старт был от root |
| `ALLOW_ROOT` | `0` | Оставить приложение под root вместо сброса привилегий |

Сборка и docker compose дополнительно параметризованы: `PYTHON_IMAGE`,
`APP_UID`, `APP_GID`, `IMAGE_NAME`, `IMAGE_TAG`, `CONTAINER_NAME`,
`CONTAINER_USER`, `VOLUME_NAME`, `RESTART_POLICY`, `BIND_ADDRESS`, `HOST_PORT`,
`READ_ONLY_ROOT`, `MEM_LIMIT`, `CPU_LIMIT`, `LOG_DRIVER`, `LOG_MAX_SIZE`,
`LOG_MAX_FILE`, `HEALTHCHECK_*`.

Группа по умолчанию — `0`, а `DATA_DIR` группе доступен на запись. Это нужно
платформам, которые назначают контейнеру произвольный UID при каждом старте:
GID остаётся нулевым, поэтому новый UID продолжает работать с той же БД.

## Развёртывание

Образ рассчитан на любой Docker-хост. Обязательных условий два: примонтировать
`DATA_DIR` и дать процессу право в него писать.

### Права на том

Большинство платформ подключает том с владельцем `root:root`, и
непривилегированный процесс писать в него не может. Поэтому образ **стартует от
root по умолчанию**: точка входа создаёт `DATA_DIR`, передаёт его
`RUN_UID:RUN_GID`, сбрасывает привилегии и только потом запускает приложение.
Под root приложение не работает никогда — если сброс не удался, точка входа
останавливается.

Никакой дополнительной настройки для обычного `docker run` не требуется:

```bash
docker run -d -v budget_data:/data -p 8080:8080 budget-manager:local
```

Если среда ограничивает capabilities, нужны `CHOWN`, `FOWNER`, `DAC_OVERRIDE`,
`SETUID` и `SETGID` — ровно они перечислены в `cap_add` в `docker-compose.yml`
поверх `cap_drop: ALL`. Само приложение не использует ни одну из них.

Когда владельцем тома управляет платформа (`fsGroup` в Kubernetes, явный
`--user` с нужным UID), root не нужен вовсе:

```bash
docker run -d -v budget_data:/data --user 10001:0 -p 8080:8080 budget-manager:local
```

В этом случае точка входа определит, что привилегий нет, и просто запустит
приложение; `cap_add` из compose можно убрать. Так же следует поступать на
платформах, которые принудительно назначают контейнеру произвольный UID: они
обязаны сами выдать том нужной группе, иначе запись невозможна в принципе.

Если каталог всё же окажется недоступным на запись, приложение остановится на
старте и сообщит, какой uid/gid у процесса и кому принадлежит каталог.

### Порт

Платформы вроде Cloud Run, Heroku и Render сообщают порт через `PORT`.
Переменная окружения перекрывает значение из образа, ничего настраивать не
нужно. `HEALTHCHECK` читает тот же `PORT`.

### HTTPS и прокси

За терминирующим TLS прокси приложение видит обычный HTTP, поэтому флаг
`Secure` на куках выставляется по `X-Forwarded-Proto`. Если прокси этот
заголовок не отправляет, задайте `COOKIE_SECURE=1` явно. Если перед
приложением нет доверенного прокси, поставьте `FORWARDED_PROTO_HEADER=`
пустым, чтобы клиент не мог влиять на решение подделкой заголовка.

Basic Authentication передаёт пароль в открытом виде, так что публиковать
сервис без TLS не следует. По умолчанию порт публикуется только на
`127.0.0.1`; `BIND_ADDRESS=0.0.0.0` открывает его наружу.

### Эфемерные платформы

Cloud Run и Heroku не дают постоянного диска: `DATA_DIR` будет писаться в
файловую систему контейнера и исчезнет при перезапуске. Для них нужен либо
внешний том, либо другое хранилище — SQLite на эфемерном диске данные не
сохранит.

### Ограничения в compose

- корневая ФС только для чтения (`READ_ONLY_ROOT=true`); запись возможна в
  `DATA_DIR` и в `tmpfs` на `/tmp`, который нужен SQLite для временных файлов;
- сняты все capabilities и запрещено повышение привилегий
  (`no-new-privileges`);
- лимиты `MEM_LIMIT` и `CPU_LIMIT`;
- логи json-file ротируются — приложение пишет в stdout строку на каждый
  запрос;
- `init: true` плюс собственная обработка `SIGTERM`: `docker stop` не ждёт
  таймаут и не обрывает запись в SQLite.

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
./run-local.sh
```

Скрипт задаёт только `DATA_DIR=./data`, чтобы не писать в `/data` на рабочей
машине; остальные значения приложение берёт из своих же умолчаний. Любую
переменную можно переопределить обычным способом:

```bash
DATA_DIR=./data PORT=9000 SEED_DEMO=0 ./run-local.sh
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
(`convert_money`), обновление курсов (`refresh_rates` с подменой сетевого
запроса) и настройки (`get_setting`/`set_setting`) — всё без обращения к сети.

Помесячный план покрыт тестами `monthly_metrics` (группировка факта по месяцам,
флаг превышения, расходы вне финансового года), `spread_evenly`,
`parse_money_or_zero`, а также проверкой автоматического обновления схемы
существующей БД при рестарте (`init_db`).

Отдельно проверяются:

- **decimal-суммы**: приведение всех представлений SQLite через `dec()`,
  точность агрегата `dsum()` на 300 суммах по `0.01`, round-trip суммы через БД
  и отсутствие в схеме колонок `*_cents` / `*_micro`;
- **WBS**: длины уровней (2/4) и лимит 15 знаков для проекта с extension,
  алфавит кодов, префикс в начале полного WBS, уникальность кода и связи
  «один WBS — один бюджет», пересборка кодов после смены префикса, список
  элементов без бюджета;
- **справочники**: идемпотентность `ref_get_or_create`, уникальность кода
  субфункции в пределах функции, соответствие спецификаций `REFERENCES` схеме и
  каталогу переводов;
- **миграция** старой БД: суммы, курсы, перенос текстовых полей в справочники,
  разбор стандартного WBS и запасной элемент для нераспознанного, идемпотентность
  повторного запуска.
