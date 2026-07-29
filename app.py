#!/usr/bin/env python3
import base64
import contextlib
import hmac
import html
import json
import os
import re
import secrets
import signal
import sqlite3
import threading
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

# --------------------------------------------------------------------------- #
# Configuration                                                                #
# --------------------------------------------------------------------------- #
# Every deployment knob is read from the environment here, so the image itself
# carries no baked-in settings and the same build runs unmodified on a laptop,
# a plain Docker host, or a PaaS that injects its own values.


def env_str(name, default):
    """Environment override, treating an empty value as unset.

    Platforms routinely hand over blank variables for unset settings; falling
    back to the default keeps that from becoming an empty hostname or path.
    """
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def env_bool(name, default):
    value = env_str(name, "1" if default else "0").lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise SystemExit(f"{name} must be a boolean (1/0, true/false, yes/no, on/off), got {value!r}")


def env_octal(name, default):
    raw = env_str(name, default)
    try:
        return int(raw, 8)
    except ValueError:
        raise SystemExit(f"{name} must be an octal file mode such as 0660, got {raw!r}")


def env_int(name, default, minimum=None, maximum=None):
    raw = env_str(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw!r}")
    if minimum is not None and value < minimum:
        raise SystemExit(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise SystemExit(f"{name} must be <= {maximum}, got {value}")
    return value


APP_NAME = env_str("APP_NAME", "Budget Control")
# Every persistent file the app owns lives under DATA_DIR, so mounting that one
# path is enough to preserve state. DB_PATH stays separately overridable for
# deployments that keep the database somewhere else entirely.
DATA_DIR = env_str("DATA_DIR", "/data")
DB_PATH = env_str("DB_PATH", os.path.join(DATA_DIR, "budget.db"))
DB_TIMEOUT = env_int("DB_TIMEOUT", 30, minimum=1)
# SQLite creates its files 0644 whatever the umask, and umask can only clear
# bits, never add them. Hosts that assign a fresh uid on every start keep the
# gid stable, so the group needs write access or the next container cannot open
# the database it just inherited.
DATA_FILE_MODE = env_octal("DATA_FILE_MODE", "0660")
HOST = env_str("HOST", "0.0.0.0")
# Hosting platforms inject the port they routed to the container, and runtime
# environment beats the image's own default.
PORT = env_int("PORT", 8080, minimum=1, maximum=65535)
APP_USER = env_str("APP_USER", "")
APP_PASSWORD = env_str("APP_PASSWORD", "")
SEED_DEMO = env_bool("SEED_DEMO", True)
# auto: mark cookies Secure only when the request arrived over HTTPS, which
# behind a terminating proxy is what X-Forwarded-Proto reports. Forcing 1 or 0
# covers proxies that do not send the header.
COOKIE_SECURE = env_str("COOKIE_SECURE", "auto").lower()
if COOKIE_SECURE not in ("auto", "1", "0", "true", "false", "yes", "no", "on", "off"):
    raise SystemExit(f"COOKIE_SECURE must be auto, 1 or 0, got {COOKIE_SECURE!r}")
# Header a terminating proxy uses to report the original scheme. Empty disables
# the lookup, which is what you want when nothing in front of the app is trusted
# to set it.
FORWARDED_PROTO_HEADER = env_str("FORWARDED_PROTO_HEADER", "X-Forwarded-Proto")

# --------------------------------------------------------------------------- #
# Internationalization (i18n)                                                  #
# --------------------------------------------------------------------------- #
LANGUAGES = ("en", "ru")                          # supported UI languages (switcher order)
LANG_COOKIE = "lang"                              # cookie remembering the visitor's choice
DEFAULT_LANG = env_str("DEFAULT_LANG", "en")      # UI language when nothing else is set
if DEFAULT_LANG not in LANGUAGES:
    raise SystemExit(f"DEFAULT_LANG must be one of {', '.join(LANGUAGES)}, got {DEFAULT_LANG!r}")

# Message catalog. Every user-visible string lives here under a dotted key, and
# both language blocks carry the SAME set of keys. The Russian block is
# annotated with an English comment on each entry so a maintainer who does not
# read Russian can still tell what every message says. Look strings up with
# t(lang, key, **kwargs); {placeholders} are substituted via str.format().
TRANSLATIONS = {
    "ru": {
        # -- top navigation & page chrome ---------------------------------- #
        "nav.overview": "Обзор",             # top nav — dashboard
        "nav.budgets": "Бюджеты",            # top nav — budgets list
        "nav.pos": "PO",                     # top nav — purchase orders
        "nav.expenses": "Расходы",           # top nav — expenses
        "nav.operations": "Операции",        # top nav — operations log
        "misc.footer": "MVP. Все суммы хранятся в минимальных денежных единицах; операции сохраняются в журнале.",  # page footer note

        # -- page headings (h1) -------------------------------------------- #
        "h1.dashboard": "Обзор бюджета",           # dashboard title
        "h1.pos": "Purchase Orders",               # purchase orders title
        "h1.expenses": "Фактические расходы",       # expenses title
        "h1.operations": "Журнал бюджетных операций",  # operations title

        # -- section headings (h2) ----------------------------------------- #
        "h2.recent": "Последние расходы",           # dashboard — recent expenses
        "h2.create_budget": "Создать бюджет",       # budgets — create form
        "h2.create_po": "Создать PO",               # pos — create form
        "h2.add_expense": "Внести расход",          # expenses — create form
        "h2.pos": "PO",                             # budget detail — POs block
        "h2.expenses": "Расходы",                   # budget detail — expenses block
        "h2.budget_operation": "Операция бюджета",  # budget detail — operation form
        "h2.operations_log": "Журнал операций",     # budget detail — operations log
        "h2.edit_po": "Редактировать PO",           # po detail — edit form
        "h2.edit_expense": "Редактировать расход",  # expense detail — edit form
        "h2.edit_operation": "Редактировать операцию",  # operation detail — edit form
        "h2.deletion": "Удаление",                  # deletion block heading

        # -- browser <title> fragments ------------------------------------- #
        "title.not_found": "Не найдено",            # 404 title
        "title.budget_edit": "Редактирование бюджета",  # budget edit title

        # -- table column headers ------------------------------------------ #
        "col.date": "Дата",                  # date
        "col.budget": "Бюджет",              # budget
        "col.description": "Описание",       # description
        "col.po": "PO",                      # purchase order
        "col.amount": "Сумма",               # amount
        "col.code": "Код",                   # budget code
        "col.year": "Год",                   # fiscal year
        "col.holder": "Budget Holder",       # budget holder (kept in English)
        "col.cost_center": "Cost Center",    # cost center (kept in English)
        "col.wbs": "WBS",                    # WBS element
        "col.ce": "CE",                      # cost element (short)
        "col.released": "Released",          # released budget
        "col.actuals": "Actuals",            # actual spend
        "col.commitments": "Commitments",    # open commitments
        "col.available": "Доступно",         # available balance
        "col.actions": "Действия",           # row actions
        "col.number": "Номер",               # PO number
        "col.vendor": "Поставщик",           # vendor
        "col.content": "Содержание",         # PO content
        "col.status": "Статус",              # status
        "col.commitment": "Commitment",      # single commitment
        "col.invoice": "Invoice",            # invoice number
        "col.operation": "Операция",         # operation type
        "col.source": "Источник",            # source budget
        "col.target": "Получатель",          # target budget
        "col.executor": "Исполнитель",       # who performed the operation
        "col.basis": "Основание",            # operation rationale

        # -- metric cards -------------------------------------------------- #
        "metric.approved": "Утверждено",                 # approved budget
        "metric.released": "Разрешено к использованию",  # released budget
        "metric.actuals": "Actuals",                     # actual spend
        "metric.commitments": "Commitments",             # open commitments
        "metric.commitment": "Commitment",               # single commitment
        "metric.available": "Доступно",                  # available balance

        # -- form field labels --------------------------------------------- #
        "label.code": "Код",                         # budget code
        "label.name": "Название",                    # budget name
        "label.fiscal_year": "Финансовый год",       # fiscal year
        "label.currency": "Валюта",                  # currency code
        "label.holder": "Budget Holder",             # budget holder
        "label.email": "Email",                      # holder e-mail
        "label.cost_element": "Cost Element",        # cost element
        "label.approved": "Утверждённый бюджет",     # approved amount
        "label.released": "Released budget",         # released amount
        "label.number": "Номер PO",                  # PO number
        "label.budget": "Бюджет",                    # budget selector
        "label.vendor": "Поставщик",                 # vendor
        "label.amount_limit": "Сумма/лимит",         # PO amount / limit
        "label.status": "Статус",                    # status selector
        "label.content": "Содержание услуг/товаров", # PO goods/services details
        "label.po": "PO",                            # PO selector
        "label.date": "Дата",                        # expense date
        "label.invoice": "Invoice",                  # invoice number
        "label.amount": "Сумма",                     # amount
        "label.description": "Описание",             # description
        "label.op_type": "Тип операции",             # operation type
        "label.target_transfer": "Целевой бюджет (Transfer/Carry forward)",  # transfer target
        "label.basis": "Основание",                  # rationale
        "label.executor": "Исполнитель",             # executor

        # -- input placeholders -------------------------------------------- #
        "ph.po_content": "Предмет, период, единицы/тариф либо максимальный лимит",  # PO description hint

        # -- <select> option labels ---------------------------------------- #
        "opt.op_supplement": "Supplement — увеличение",    # operation: supplement
        "opt.op_reduction": "Reduction — сокращение",      # operation: reduction
        "opt.op_release": "Release — разблокировка",       # operation: release
        "opt.op_return": "Return — возврат",               # operation: return
        "opt.op_transfer": "Transfer — перенос",           # operation: transfer
        "opt.op_carry": "Carry forward",                   # operation: carry forward
        "opt.po_draft": "Draft — без резерва",             # PO status: draft
        "opt.po_approved": "Approved — резервировать",     # PO status: approved
        "opt.available": "{code} — доступно {money}",      # budget option: "<code> — available <sum>"
        "opt.remaining": "{number} — остаток {money}",     # PO option: "<number> — remaining <sum>"

        # -- buttons ------------------------------------------------------- #
        "btn.create_budget": "Создать бюджет",       # submit new budget
        "btn.create_po": "Создать PO",               # submit new PO
        "btn.run_operation": "Провести операцию",    # submit operation
        "btn.post_expense": "Провести расход",       # submit expense
        "btn.open": "Открыть",                       # open detail
        "btn.save": "Сохранить",                     # save edit
        "btn.save_changes": "Сохранить изменения",   # save budget edit
        "btn.edit": "Редактировать",                 # go to edit page
        "btn.delete_budget": "Удалить бюджет",       # delete budget
        "btn.delete_po": "Удалить PO",               # delete PO
        "btn.delete_expense": "Удалить расход",      # delete expense
        "btn.delete_operation": "Удалить операцию",  # delete operation
        "btn.back_to_budget": "К бюджету",           # back to budget detail
        "btn.back_to_pos": "К списку PO",            # back to PO list
        "btn.back_to_expenses": "К расходам",        # back to expenses
        "btn.back_to_operations": "К журналу",       # back to operations log

        # -- PO status actions --------------------------------------------- #
        "action.approve": "Утвердить",               # approve PO
        "action.cancel": "Отменить",                 # cancel PO
        "action.close": "Закрыть остаток",           # close PO remainder

        # -- empty-table placeholders -------------------------------------- #
        "empty.recent": "Расходов нет",              # no recent expenses
        "empty.pos": "PO отсутствуют",               # no POs
        "empty.expenses": "Расходы отсутствуют",     # no expenses
        "empty.operations": "Операции отсутствуют",  # no operations

        # -- misc inline text ---------------------------------------------- #
        "misc.no_po": "Без PO",                      # "no PO" marker / option
        "misc.page_not_found": "Страница не найдена.",  # 404 body
        "misc.budget_not_found": "Бюджет не найден",    # missing budget heading
        "misc.po_not_found": "PO не найден",            # missing PO heading
        "misc.expense_not_found": "Расход не найден",   # missing expense heading
        "misc.operation_not_found": "Операция не найдена",  # missing operation heading
        "misc.edit_budget_note": "Изменяются базовые (initial) значения. Итоговые approved/released также учитывают проведённые операции.",  # budget edit hint
        "misc.po_not_editable": "PO в статусе {status} нельзя редактировать.",  # PO not editable notice
        "misc.po_delete_blocked": "Удаление недоступно: по PO проведено расходов — {count}. Сначала удалите связанные расходы.",  # PO delete blocked
        "misc.budget_delete_blocked": "Удаление недоступно: с бюджетом связаны PO, расходы или операции ({linked}). Сначала удалите связанные записи.",  # budget delete blocked
        "misc.po_meta": "Бюджет: {budget} · Поставщик: {vendor}",  # PO detail summary (budget is an HTML link)
        "misc.edit_op_note": "Бюджет-источник изменить нельзя. Новые значения пересчитываются и проверяются на допустимость.",  # operation edit hint
        "misc.op_meta": "Создано: {created_at} · Исполнитель: {created_by}",  # operation detail meta line
        "misc.op_source": "Источник: {source}",      # operation detail — source (HTML link)
        "misc.op_target": " · Получатель: {target}", # operation detail — target suffix
        "misc.h1_budget_edit": "Редактирование бюджета {code}",  # budget edit heading
        "misc.h1_po": "PO {number}",                 # PO detail heading
        "misc.h1_expense": "Расход #{id}",           # expense detail heading
        "misc.h1_operation": "Операция #{id}",       # operation detail heading

        # -- flash (success) messages -------------------------------------- #
        "flash.budget_created": "Бюджет создан",             # budget created
        "flash.operation_done": "Операция проведена",        # operation posted
        "flash.po_created": "PO создан",                     # PO created
        "flash.po_status_changed": "Статус PO изменён",      # PO status changed
        "flash.expense_posted": "Расход проведён",           # expense posted
        "flash.budget_updated": "Бюджет обновлён",           # budget updated
        "flash.budget_deleted": "Бюджет удалён",             # budget deleted
        "flash.po_updated": "PO обновлён",                   # PO updated
        "flash.po_deleted": "PO удалён",                     # PO deleted
        "flash.expense_updated": "Расход обновлён",          # expense updated
        "flash.expense_deleted": "Расход удалён",            # expense deleted
        "flash.operation_updated": "Операция обновлена",     # operation updated
        "flash.operation_deleted": "Операция удалена",       # operation deleted

        # -- error messages ------------------------------------------------ #
        "error.bad_amount": "Некорректная сумма",                          # amount not parseable
        "error.amount_positive": "Сумма должна быть больше нуля",           # amount <= 0
        "error.bad_field": "Некорректное значение поля «{label}»",          # int parse failed
        "error.bad_date": "Некорректная дата",                             # date parse failed
        "error.field_required": "Поле «{label}» обязательно",              # required field empty
        "error.request_too_large": "Слишком большой запрос",               # POST body too large
        "error.csrf": "Ошибка CSRF. Обновите страницу и повторите действие",  # CSRF token mismatch
        "error.released_exceeds_approved": "Бюджет {code}: released превысил бы approved",  # invariant: released>approved
        "error.available_negative": "Бюджет {code}: доступный остаток стал бы отрицательным",  # invariant: available<0
        "error.reduction_negative": "Сокращение сделает доступный бюджет отрицательным",  # reduction below used
        "error.release_exceeds": "Release превышает утверждённый бюджет",   # release beyond approved
        "error.return_used": "Нельзя вернуть уже использованный или зарезервированный бюджет",  # return below used
        "error.target_not_found": "Целевой бюджет не найден",              # transfer target missing
        "error.currency_mismatch": "Перенос между разными валютами не поддерживается",  # cross-currency transfer
        "error.insufficient_transfer": "Недостаточно свободного бюджета для переноса",  # transfer too big
        "error.carry_forward_year": "Carry forward должен идти в более поздний финансовый год",  # carry-forward direction
        "error.unknown_operation": "Неизвестный тип операции",             # unknown operation code
        "error.unknown_action": "Неизвестная операция",                    # unknown POST route
        "error.internal": "Внутренняя ошибка",                             # unexpected exception
        "error.released_gt_approved_input": "Released budget не может превышать утверждённый",  # form: released>approved
        "error.currency_format": "Валюта должна быть трёхбуквенным кодом",  # currency not 3 letters
        "error.budget_not_found": "Бюджет не найден",                      # budget lookup failed
        "error.choose_other_target": "Укажите другой целевой бюджет",       # transfer target = source
        "error.insufficient_po_approve": "Недостаточно доступного бюджета для утверждения PO",  # approve PO too big
        "error.bad_po_status": "Некорректный статус PO",                   # invalid new PO status (create)
        "error.bad_status": "Некорректный статус",                         # invalid status transition
        "error.po_not_found": "PO не найден",                              # PO lookup failed
        "error.approve_only_draft": "Утвердить можно только Draft PO",      # approve non-draft
        "error.insufficient_available": "Недостаточно доступного бюджета",  # not enough available
        "error.po_already_closed": "PO уже закрыт",                        # close/cancel closed PO
        "error.po_not_in_budget": "PO не относится к выбранному бюджету",   # PO/budget mismatch
        "error.expense_needs_approved_po": "Расход можно провести только по утверждённому PO",  # expense on non-approved PO
        "error.expense_exceeds_po": "Расход превышает остаток PO",          # expense over PO remainder
        "error.insufficient_no_po": "Недостаточно доступного бюджета для расхода без PO",  # no-PO expense too big
        "error.edit_only_draft_approved": "Редактировать можно только Draft или Approved PO",  # edit closed PO
        "error.po_amount_lt_spent": "Сумма PO не может быть меньше уже проведённых по нему расходов",  # PO amount < spent
        "error.cannot_change_budget_with_expenses": "Нельзя сменить бюджет у PO с проведёнными расходами",  # rebudget PO with expenses
        "error.cannot_delete_po_with_expenses": "Нельзя удалить PO с проведёнными расходами",  # delete PO with expenses
        "error.expense_not_found": "Расход не найден",                     # expense lookup failed
        "error.operation_not_found": "Операция не найдена",                # operation lookup failed
        "error.source_not_found": "Бюджет-источник не найден",             # operation source missing
        "error.cannot_delete_budget_linked": "Нельзя удалить бюджет со связанными PO, расходами или операциями",  # delete linked budget

        # -- field names embedded into error messages ---------------------- #
        "field.code": "Код",                     # budget code
        "field.name": "Название",                # budget name
        "field.holder": "Budget Holder",         # budget holder
        "field.fiscal_year": "финансовый год",   # fiscal year (lowercase in-sentence)
        "field.vendor": "Поставщик",             # vendor
        "field.content": "Содержание",           # PO content
        "field.budget": "бюджет",                # budget (lowercase in-sentence)
        "field.po_number": "Номер PO",           # PO number
        "field.target_budget": "целевой бюджет", # target budget (lowercase in-sentence)
        "field.po": "PO",                        # purchase order
        "field.description": "Описание",         # description
    },
    "en": {
        # top navigation & page chrome
        "nav.overview": "Overview",
        "nav.budgets": "Budgets",
        "nav.pos": "PO",
        "nav.expenses": "Expenses",
        "nav.operations": "Operations",
        "misc.footer": "MVP. All amounts are stored in minor currency units; operations are kept in an audit log.",

        # page headings (h1)
        "h1.dashboard": "Budget overview",
        "h1.pos": "Purchase Orders",
        "h1.expenses": "Actual expenses",
        "h1.operations": "Budget operations log",

        # section headings (h2)
        "h2.recent": "Recent expenses",
        "h2.create_budget": "Create budget",
        "h2.create_po": "Create PO",
        "h2.add_expense": "Add expense",
        "h2.pos": "PO",
        "h2.expenses": "Expenses",
        "h2.budget_operation": "Budget operation",
        "h2.operations_log": "Operations log",
        "h2.edit_po": "Edit PO",
        "h2.edit_expense": "Edit expense",
        "h2.edit_operation": "Edit operation",
        "h2.deletion": "Deletion",

        # browser <title> fragments
        "title.not_found": "Not found",
        "title.budget_edit": "Edit budget",

        # table column headers
        "col.date": "Date",
        "col.budget": "Budget",
        "col.description": "Description",
        "col.po": "PO",
        "col.amount": "Amount",
        "col.code": "Code",
        "col.year": "Year",
        "col.holder": "Budget Holder",
        "col.cost_center": "Cost Center",
        "col.wbs": "WBS",
        "col.ce": "CE",
        "col.released": "Released",
        "col.actuals": "Actuals",
        "col.commitments": "Commitments",
        "col.available": "Available",
        "col.actions": "Actions",
        "col.number": "Number",
        "col.vendor": "Vendor",
        "col.content": "Details",
        "col.status": "Status",
        "col.commitment": "Commitment",
        "col.invoice": "Invoice",
        "col.operation": "Operation",
        "col.source": "Source",
        "col.target": "Target",
        "col.executor": "Executor",
        "col.basis": "Rationale",

        # metric cards
        "metric.approved": "Approved",
        "metric.released": "Released for use",
        "metric.actuals": "Actuals",
        "metric.commitments": "Commitments",
        "metric.commitment": "Commitment",
        "metric.available": "Available",

        # form field labels
        "label.code": "Code",
        "label.name": "Name",
        "label.fiscal_year": "Fiscal year",
        "label.currency": "Currency",
        "label.holder": "Budget Holder",
        "label.email": "Email",
        "label.cost_element": "Cost Element",
        "label.approved": "Approved budget",
        "label.released": "Released budget",
        "label.number": "PO number",
        "label.budget": "Budget",
        "label.vendor": "Vendor",
        "label.amount_limit": "Amount / limit",
        "label.status": "Status",
        "label.content": "Goods / services details",
        "label.po": "PO",
        "label.date": "Date",
        "label.invoice": "Invoice",
        "label.amount": "Amount",
        "label.description": "Description",
        "label.op_type": "Operation type",
        "label.target_transfer": "Target budget (Transfer/Carry forward)",
        "label.basis": "Rationale",
        "label.executor": "Executor",

        # input placeholders
        "ph.po_content": "Subject, period, units/rate or a maximum limit",

        # <select> option labels
        "opt.op_supplement": "Supplement — increase",
        "opt.op_reduction": "Reduction — decrease",
        "opt.op_release": "Release — unlock",
        "opt.op_return": "Return",
        "opt.op_transfer": "Transfer",
        "opt.op_carry": "Carry forward",
        "opt.po_draft": "Draft — no reservation",
        "opt.po_approved": "Approved — reserve",
        "opt.available": "{code} — available {money}",
        "opt.remaining": "{number} — remaining {money}",

        # buttons
        "btn.create_budget": "Create budget",
        "btn.create_po": "Create PO",
        "btn.run_operation": "Post operation",
        "btn.post_expense": "Post expense",
        "btn.open": "Open",
        "btn.save": "Save",
        "btn.save_changes": "Save changes",
        "btn.edit": "Edit",
        "btn.delete_budget": "Delete budget",
        "btn.delete_po": "Delete PO",
        "btn.delete_expense": "Delete expense",
        "btn.delete_operation": "Delete operation",
        "btn.back_to_budget": "Back to budget",
        "btn.back_to_pos": "Back to POs",
        "btn.back_to_expenses": "Back to expenses",
        "btn.back_to_operations": "Back to log",

        # PO status actions
        "action.approve": "Approve",
        "action.cancel": "Cancel",
        "action.close": "Close remainder",

        # empty-table placeholders
        "empty.recent": "No expenses",
        "empty.pos": "No POs",
        "empty.expenses": "No expenses",
        "empty.operations": "No operations",

        # misc inline text
        "misc.no_po": "No PO",
        "misc.page_not_found": "Page not found.",
        "misc.budget_not_found": "Budget not found",
        "misc.po_not_found": "PO not found",
        "misc.expense_not_found": "Expense not found",
        "misc.operation_not_found": "Operation not found",
        "misc.edit_budget_note": "The base (initial) values are edited. Effective approved/released also account for posted operations.",
        "misc.po_not_editable": "A PO with status {status} cannot be edited.",
        "misc.po_delete_blocked": "Deletion unavailable: the PO has {count} posted expense(s). Delete the linked expenses first.",
        "misc.budget_delete_blocked": "Deletion unavailable: the budget has linked POs, expenses or operations ({linked}). Delete the linked records first.",
        "misc.po_meta": "Budget: {budget} · Vendor: {vendor}",
        "misc.edit_op_note": "The source budget cannot be changed. New values are recomputed and revalidated.",
        "misc.op_meta": "Created: {created_at} · Executor: {created_by}",
        "misc.op_source": "Source: {source}",
        "misc.op_target": " · Target: {target}",
        "misc.h1_budget_edit": "Edit budget {code}",
        "misc.h1_po": "PO {number}",
        "misc.h1_expense": "Expense #{id}",
        "misc.h1_operation": "Operation #{id}",

        # flash (success) messages
        "flash.budget_created": "Budget created",
        "flash.operation_done": "Operation posted",
        "flash.po_created": "PO created",
        "flash.po_status_changed": "PO status changed",
        "flash.expense_posted": "Expense posted",
        "flash.budget_updated": "Budget updated",
        "flash.budget_deleted": "Budget deleted",
        "flash.po_updated": "PO updated",
        "flash.po_deleted": "PO deleted",
        "flash.expense_updated": "Expense updated",
        "flash.expense_deleted": "Expense deleted",
        "flash.operation_updated": "Operation updated",
        "flash.operation_deleted": "Operation deleted",

        # error messages
        "error.bad_amount": "Invalid amount",
        "error.amount_positive": "Amount must be greater than zero",
        "error.bad_field": "Invalid value for field “{label}”",
        "error.bad_date": "Invalid date",
        "error.field_required": "Field “{label}” is required",
        "error.request_too_large": "Request too large",
        "error.csrf": "CSRF error. Refresh the page and try again",
        "error.released_exceeds_approved": "Budget {code}: released would exceed approved",
        "error.available_negative": "Budget {code}: available balance would go negative",
        "error.reduction_negative": "Reduction would make the available budget negative",
        "error.release_exceeds": "Release exceeds the approved budget",
        "error.return_used": "Cannot return budget already spent or committed",
        "error.target_not_found": "Target budget not found",
        "error.currency_mismatch": "Transfers between different currencies are not supported",
        "error.insufficient_transfer": "Not enough free budget to transfer",
        "error.carry_forward_year": "Carry forward must target a later fiscal year",
        "error.unknown_operation": "Unknown operation type",
        "error.unknown_action": "Unknown action",
        "error.internal": "Internal error",
        "error.released_gt_approved_input": "Released budget cannot exceed the approved budget",
        "error.currency_format": "Currency must be a three-letter code",
        "error.budget_not_found": "Budget not found",
        "error.choose_other_target": "Choose a different target budget",
        "error.insufficient_po_approve": "Not enough available budget to approve the PO",
        "error.bad_po_status": "Invalid PO status",
        "error.bad_status": "Invalid status",
        "error.po_not_found": "PO not found",
        "error.approve_only_draft": "Only a Draft PO can be approved",
        "error.insufficient_available": "Not enough available budget",
        "error.po_already_closed": "PO is already closed",
        "error.po_not_in_budget": "PO does not belong to the selected budget",
        "error.expense_needs_approved_po": "An expense can be posted only against an approved PO",
        "error.expense_exceeds_po": "Expense exceeds the PO remaining amount",
        "error.insufficient_no_po": "Not enough available budget for an expense without a PO",
        "error.edit_only_draft_approved": "Only Draft or Approved POs can be edited",
        "error.po_amount_lt_spent": "PO amount cannot be less than expenses already posted against it",
        "error.cannot_change_budget_with_expenses": "Cannot change the budget of a PO that has posted expenses",
        "error.cannot_delete_po_with_expenses": "Cannot delete a PO with posted expenses",
        "error.expense_not_found": "Expense not found",
        "error.operation_not_found": "Operation not found",
        "error.source_not_found": "Source budget not found",
        "error.cannot_delete_budget_linked": "Cannot delete a budget with linked POs, expenses or operations",

        # field names embedded into error messages
        "field.code": "Code",
        "field.name": "Name",
        "field.holder": "Budget Holder",
        "field.fiscal_year": "fiscal year",
        "field.vendor": "Vendor",
        "field.content": "Details",
        "field.budget": "budget",
        "field.po_number": "PO number",
        "field.target_budget": "target budget",
        "field.po": "PO",
        "field.description": "Description",
    },
}

# Currency / settings strings. Kept in a separate block (merged into
# TRANSLATIONS below) so the multi-currency feature adds its own keys without
# editing the large catalog above. Both language blocks carry the same keys.
_CURRENCY_TRANSLATIONS = {
    "ru": {
        "nav.settings": "Настройки",                       # top nav — settings
        "h1.settings": "Настройки",                        # settings page title
        "h2.base_currency": "Основная валюта",             # settings — base currency block
        "h2.currencies": "Валюты",                         # settings — currency catalog block
        "h2.cbr_rates": "Курсы ЦБ РФ",                     # settings — CBR rates block
        "label.base_currency": "Основная валюта отображения",  # base currency selector
        "col.currency": "Валюта",                          # currency code column
        "col.name": "Название",                            # currency name column
        "col.rate": "Курс к RUB",                          # rate-to-RUB column
        "col.rate_updated": "Обновлён",                    # rate updated-at column
        "col.active": "Активна",                           # is-active column
        "btn.save_settings": "Сохранить настройки",        # save settings
        "btn.refresh_rates": "Обновить курсы ЦБ РФ",       # trigger CBR refresh
        "cur.display": "Валюта отображения",               # display-currency switcher label
        "cur.no_rate": "нет курса",                        # shown when a rate is missing
        "misc.rates_updated_at": "Курсы обновлены: {when}",   # rates last-updated line
        "misc.rates_never": "Курсы ещё не загружались",       # rates never fetched
        "misc.dashboard_no_rate": "Нет курса для: {codes}. Обновите курсы ЦБ РФ.",  # dashboard warning
        "misc.base_currency_hint": "Все документы по умолчанию показываются в этой валюте.",  # base currency hint
        "flash.settings_saved": "Настройки сохранены",        # settings saved
        "flash.rates_refreshed": "Курсы ЦБ РФ обновлены ({count})",  # rates refreshed
        "error.currency_not_active": "Валюта не активна",     # inactive currency submitted
        "error.base_must_be_active": "Основная валюта должна быть активной",  # base deactivated
        "error.base_currency_unknown": "Неизвестная основная валюта",  # unknown base
        "error.cbr_fetch": "Не удалось получить курсы ЦБ РФ: {detail}",  # CBR fetch/parse failed
    },
    "en": {
        "nav.settings": "Settings",
        "h1.settings": "Settings",
        "h2.base_currency": "Base currency",
        "h2.currencies": "Currencies",
        "h2.cbr_rates": "CBR exchange rates",
        "label.base_currency": "Base display currency",
        "col.currency": "Currency",
        "col.name": "Name",
        "col.rate": "Rate to RUB",
        "col.rate_updated": "Updated",
        "col.active": "Active",
        "btn.save_settings": "Save settings",
        "btn.refresh_rates": "Refresh CBR rates",
        "cur.display": "Display currency",
        "cur.no_rate": "no rate",
        "misc.rates_updated_at": "Rates updated: {when}",
        "misc.rates_never": "Rates have not been fetched yet",
        "misc.dashboard_no_rate": "No rate for: {codes}. Refresh the CBR rates.",
        "misc.base_currency_hint": "All documents are shown in this currency by default.",
        "flash.settings_saved": "Settings saved",
        "flash.rates_refreshed": "CBR rates refreshed ({count})",
        "error.currency_not_active": "Currency is not active",
        "error.base_must_be_active": "The base currency must be active",
        "error.base_currency_unknown": "Unknown base currency",
        "error.cbr_fetch": "Failed to fetch CBR rates: {detail}",
    },
}
for _lang, _msgs in _CURRENCY_TRANSLATIONS.items():
    TRANSLATIONS.setdefault(_lang, {}).update(_msgs)

# Monthly budgeting strings. Same pattern as _CURRENCY_TRANSLATIONS: the
# feature contributes its own keys without editing the large catalog above.
_MONTHLY_TRANSLATIONS = {
    "ru": {
        "h2.monthly": "Помесячный план",                     # budget detail — monthly breakdown block
        "col.month": "Месяц",                                # month column
        "col.allocated": "План",                             # allocated amount column
        "col.remaining": "Остаток",                          # remaining amount column
        "month.1": "Январь",                                 # January
        "month.2": "Февраль",                                # February
        "month.3": "Март",                                   # March
        "month.4": "Апрель",                                 # April
        "month.5": "Май",                                    # May
        "month.6": "Июнь",                                   # June
        "month.7": "Июль",                                   # July
        "month.8": "Август",                                 # August
        "month.9": "Сентябрь",                               # September
        "month.10": "Октябрь",                               # October
        "month.11": "Ноябрь",                                # November
        "month.12": "Декабрь",                               # December
        "misc.total": "Итого",                               # totals row label
        "misc.no_monthly_plan": "План по месяцам не задан — контроль только по годовому бюджету.",  # no monthly plan note
        "misc.alloc_note": "Пустое поле = 0. Очистите все поля, чтобы удалить помесячный план. Превышение плана месяца не блокирует расходы — жёстко контролируется только годовой бюджет.",  # allocation form hint
        "misc.alloc_vs_released": "Сумма плана ({allocated}) не совпадает с released-бюджетом ({released}).",  # plan total mismatch warning
        "misc.out_of_year_actuals": "Расходы вне финансового года {year}: {money}.",  # out-of-fiscal-year actuals note
        "status.month_ok": "OK",                             # month within plan
        "status.month_over": "Превышение",                   # month over plan
        "btn.save_allocations": "Сохранить план",            # save monthly plan
        "btn.distribute_evenly": "Распределить равномерно",  # spread released budget over 12 months
        "btn.apply_filter": "Фильтр",                        # apply month filter
        "btn.clear_filter": "Сброс",                         # clear month filter
        "flash.allocations_saved": "Помесячный план сохранён",  # allocations saved
        "flash.allocations_cleared": "Помесячный план удалён",  # allocations cleared
        "flash.expense_posted_over_month": "Расход проведён — превышен план месяца",   # posted, month over plan
        "flash.expense_updated_over_month": "Расход обновлён — превышен план месяца",  # updated, month over plan
    },
    "en": {
        "h2.monthly": "Monthly plan",
        "col.month": "Month",
        "col.allocated": "Planned",
        "col.remaining": "Remaining",
        "month.1": "January",
        "month.2": "February",
        "month.3": "March",
        "month.4": "April",
        "month.5": "May",
        "month.6": "June",
        "month.7": "July",
        "month.8": "August",
        "month.9": "September",
        "month.10": "October",
        "month.11": "November",
        "month.12": "December",
        "misc.total": "Total",
        "misc.no_monthly_plan": "No monthly plan is set — only the annual budget is controlled.",
        "misc.alloc_note": "Blank field = 0. Clear all fields to remove the monthly plan. Exceeding a month's plan does not block expenses — only the annual budget is enforced.",
        "misc.alloc_vs_released": "Planned total ({allocated}) differs from the released budget ({released}).",
        "misc.out_of_year_actuals": "Actuals outside fiscal year {year}: {money}.",
        "status.month_ok": "OK",
        "status.month_over": "Over plan",
        "btn.save_allocations": "Save plan",
        "btn.distribute_evenly": "Distribute evenly",
        "btn.apply_filter": "Filter",
        "btn.clear_filter": "Reset",
        "flash.allocations_saved": "Monthly plan saved",
        "flash.allocations_cleared": "Monthly plan removed",
        "flash.expense_posted_over_month": "Expense posted — month plan exceeded",
        "flash.expense_updated_over_month": "Expense updated — month plan exceeded",
    },
}
for _lang, _msgs in _MONTHLY_TRANSLATIONS.items():
    TRANSLATIONS.setdefault(_lang, {}).update(_msgs)

# Reference-data strings. Same pattern as the two blocks above: the feature
# contributes its own keys without editing the large catalog at the top.
_HANDBOOK_TRANSLATIONS = {
    "ru": {
        "nav.handbooks": "Справочники",
        "h1.handbooks": "Справочники",
        "misc.handbooks_intro": "Цепочка: юр. лицо → центр затрат (CC) → WBS → элемент затрат (CE). Код каждой записи уникален внутри своего справочника.",
        "hb.le": "Юридические лица",
        "hb.cc": "Центры затрат (CC)",
        "hb.wbs": "WBS",
        "hb.ce": "Элементы затрат (CE)",
        "hb.full": "Full WBS",
        "hb.le_desc": "Юр. лица, код которых становится префиксом full WBS.",
        "hb.cc_desc": "Центры затрат. Каждый принадлежит одному юр. лицу.",
        "hb.wbs_desc": "WBS вида функция/подфункция/проект.расширение. Каждый принадлежит одному CC.",
        "hb.ce_desc": "Элементы затрат. Привязываются к WBS многие-ко-многим.",
        "hb.full_desc": "Сводный реестр: код юр. лица + код WBS. Вычисляется автоматически.",
        "h1.legal_entities": "Юридические лица",
        "h1.cost_centers": "Центры затрат",
        "h1.wbs": "WBS",
        "h1.cost_elements": "Элементы затрат",
        "h1.full_wbs": "Реестр Full WBS",
        "h2.create_le": "Новое юр. лицо",
        "h2.create_cc": "Новый центр затрат",
        "h2.create_wbs": "Новый WBS",
        "h2.create_ce": "Новый элемент затрат",
        "h2.cc_of_le": "Центры затрат",
        "h2.wbs_of_le": "WBS юр. лица",
        "h2.wbs_of_cc": "WBS центра затрат",
        "h2.wbs_of_ce": "WBS с этим элементом затрат",
        "h2.budgets_of_wbs": "Бюджетные строки",
        "h2.edit_ce_links": "Элементы затрат",
        "col.legal_entity": "Юр. лицо",
        "col.full_wbs": "Full WBS",
        "col.cc_count": "CC",
        "col.wbs_count": "WBS",
        "col.budgets": "Бюджетов",
        "col.ce_list": "Элементы затрат",
        "label.legal_entity": "Юр. лицо",
        "label.cost_center_sel": "Центр затрат",
        "label.wbs_sel": "WBS",
        "label.cost_elements": "Элементы затрат",
        "btn.create_le": "Создать юр. лицо",
        "btn.create_cc": "Создать центр затрат",
        "btn.create_wbs": "Создать WBS",
        "btn.create_ce": "Создать элемент затрат",
        "btn.delete_le": "Удалить юр. лицо",
        "btn.delete_cc": "Удалить центр затрат",
        "btn.delete_wbs": "Удалить WBS",
        "btn.delete_ce": "Удалить элемент затрат",
        "btn.back_to_le": "К юр. лицам",
        "btn.back_to_cc": "К центрам затрат",
        "btn.back_to_wbs": "К WBS",
        "btn.back_to_ce": "К элементам затрат",
        "btn.back_to_handbooks": "К справочникам",
        "title.le_edit": "Изменение юр. лица",
        "title.cc_edit": "Изменение центра затрат",
        "title.wbs_edit": "Изменение WBS",
        "title.ce_edit": "Изменение элемента затрат",
        "misc.h1_le_edit": "Юр. лицо {code}",
        "misc.h1_cc_edit": "Центр затрат {code}",
        "misc.h1_wbs_edit": "WBS {code}",
        "misc.h1_ce_edit": "Элемент затрат {code}",
        "misc.le_not_found": "Юр. лицо не найдено",
        "misc.cc_not_found": "Центр затрат не найден",
        "misc.wbs_not_found": "WBS не найден",
        "misc.ce_not_found": "Элемент затрат не найден",
        "misc.le_delete_blocked": "Удаление недоступно: к юр. лицу привязаны центры затрат ({linked}).",
        "misc.cc_delete_blocked": "Удаление недоступно: к центру затрат привязаны WBS ({linked}).",
        "misc.wbs_delete_blocked": "Удаление недоступно: на WBS ссылаются бюджетные строки ({linked}).",
        "misc.ce_delete_blocked": "Удаление недоступно: элемент затрат используется ({linked}).",
        "misc.entity_code_hint": "Латиница и цифры, без «/». Пример: RU12.",
        "misc.wbs_code_hint": "Формат: функция/подфункция/проект.расширение. Пример: IT/Infr/DC.DCT3srv (расширение необязательно).",
        "misc.wbs_move_warning": "На этом WBS есть бюджетные строки ({linked}). Смена центра затрат может изменить юр. лицо, а значит и full WBS.",
        "misc.full_wbs_note": "Реестр вычисляется автоматически из кода юр. лица и кода WBS. Записи заводятся и изменяются в справочнике WBS.",
        "misc.wbs_meta": "Full WBS: {full} · юр. лицо: {le} · центр затрат: {cc}",
        "misc.no_wbs": "не задан",
        "misc.budget_wbs_missing": "У бюджета не выбран WBS — укажите его при редактировании.",
        "empty.legal_entities": "Юр. лиц пока нет",
        "empty.cost_centers": "Центров затрат пока нет",
        "empty.wbs": "WBS пока нет",
        "empty.cost_elements": "Элементов затрат пока нет",
        "empty.full_wbs": "Реестр пуст",
        "empty.wbs_ce": "Элементы затрат не привязаны",
        "empty.budgets": "Бюджетных строк нет",
        "filter.legal_entity": "Юр. лицо",
        "filter.cost_center": "Центр затрат",
        "filter.all": "Все",
        "flash.le_created": "Юр. лицо создано",
        "flash.le_updated": "Юр. лицо обновлено",
        "flash.le_deleted": "Юр. лицо удалено",
        "flash.cc_created": "Центр затрат создан",
        "flash.cc_updated": "Центр затрат обновлён",
        "flash.cc_deleted": "Центр затрат удалён",
        "flash.wbs_created": "WBS создан",
        "flash.wbs_updated": "WBS обновлён",
        "flash.wbs_deleted": "WBS удалён",
        "flash.ce_created": "Элемент затрат создан",
        "flash.ce_updated": "Элемент затрат обновлён",
        "flash.ce_deleted": "Элемент затрат удалён",
        "field.entity_code": "код юр. лица",
        "field.wbs_code": "код WBS",
        "error.entity_code_format": "Код юр. лица: латиница и цифры, 2–16 символов, без «/»",
        "error.ref_code_format": "Код: латиница, цифры, точка, дефис или подчёркивание, до 32 символов, без «/»",
        "error.wbs_code_format": "Код WBS должен быть вида функция/подфункция/проект.расширение, например IT/Infr/DC.DCT3srv",
        "error.le_not_found": "Юр. лицо не найдено",
        "error.cc_not_found": "Центр затрат не найден",
        "error.wbs_not_found": "WBS не найден",
        "error.ce_not_found": "Элемент затрат не найден",
        "error.cannot_delete_le_linked": "Нельзя удалить юр. лицо: к нему привязаны центры затрат",
        "error.cannot_delete_cc_linked": "Нельзя удалить центр затрат: к нему привязаны WBS",
        "error.cannot_delete_wbs_linked": "Нельзя удалить WBS: на него ссылаются бюджетные строки",
        "error.cannot_delete_ce_linked": "Нельзя удалить элемент затрат: он используется",
        "error.ce_not_in_wbs": "Элемент затрат не привязан к выбранному WBS",
        "error.wbs_required": "Выберите WBS",
    },
    "en": {
        "nav.handbooks": "Handbooks",
        "h1.handbooks": "Handbooks",
        "misc.handbooks_intro": "The chain is legal entity → cost centre (CC) → WBS → cost element (CE). Every code is unique inside its own handbook.",
        "hb.le": "Legal entities",
        "hb.cc": "Cost centres (CC)",
        "hb.wbs": "WBS",
        "hb.ce": "Cost elements (CE)",
        "hb.full": "Full WBS",
        "hb.le_desc": "Legal entities, whose code becomes the full WBS prefix.",
        "hb.cc_desc": "Cost centres. Each belongs to one legal entity.",
        "hb.wbs_desc": "WBS of the form function/subfunction/project.extension. Each belongs to one CC.",
        "hb.ce_desc": "Cost elements. Linked to WBS many-to-many.",
        "hb.full_desc": "Combined register: legal entity code + WBS code. Computed automatically.",
        "h1.legal_entities": "Legal entities",
        "h1.cost_centers": "Cost centres",
        "h1.wbs": "WBS",
        "h1.cost_elements": "Cost elements",
        "h1.full_wbs": "Full WBS register",
        "h2.create_le": "New legal entity",
        "h2.create_cc": "New cost centre",
        "h2.create_wbs": "New WBS",
        "h2.create_ce": "New cost element",
        "h2.cc_of_le": "Cost centres",
        "h2.wbs_of_le": "WBS of the legal entity",
        "h2.wbs_of_cc": "WBS of the cost centre",
        "h2.wbs_of_ce": "WBS using this cost element",
        "h2.budgets_of_wbs": "Budget lines",
        "h2.edit_ce_links": "Cost elements",
        "col.legal_entity": "Legal entity",
        "col.full_wbs": "Full WBS",
        "col.cc_count": "CC",
        "col.wbs_count": "WBS",
        "col.budgets": "Budgets",
        "col.ce_list": "Cost elements",
        "label.legal_entity": "Legal entity",
        "label.cost_center_sel": "Cost centre",
        "label.wbs_sel": "WBS",
        "label.cost_elements": "Cost elements",
        "btn.create_le": "Create legal entity",
        "btn.create_cc": "Create cost centre",
        "btn.create_wbs": "Create WBS",
        "btn.create_ce": "Create cost element",
        "btn.delete_le": "Delete legal entity",
        "btn.delete_cc": "Delete cost centre",
        "btn.delete_wbs": "Delete WBS",
        "btn.delete_ce": "Delete cost element",
        "btn.back_to_le": "Back to legal entities",
        "btn.back_to_cc": "Back to cost centres",
        "btn.back_to_wbs": "Back to WBS",
        "btn.back_to_ce": "Back to cost elements",
        "btn.back_to_handbooks": "Back to handbooks",
        "title.le_edit": "Edit legal entity",
        "title.cc_edit": "Edit cost centre",
        "title.wbs_edit": "Edit WBS",
        "title.ce_edit": "Edit cost element",
        "misc.h1_le_edit": "Legal entity {code}",
        "misc.h1_cc_edit": "Cost centre {code}",
        "misc.h1_wbs_edit": "WBS {code}",
        "misc.h1_ce_edit": "Cost element {code}",
        "misc.le_not_found": "Legal entity not found",
        "misc.cc_not_found": "Cost centre not found",
        "misc.wbs_not_found": "WBS not found",
        "misc.ce_not_found": "Cost element not found",
        "misc.le_delete_blocked": "Deletion unavailable: the legal entity has cost centres ({linked}).",
        "misc.cc_delete_blocked": "Deletion unavailable: the cost centre has WBS ({linked}).",
        "misc.wbs_delete_blocked": "Deletion unavailable: budget lines reference this WBS ({linked}).",
        "misc.ce_delete_blocked": "Deletion unavailable: the cost element is in use ({linked}).",
        "misc.entity_code_hint": "Letters and digits, no \"/\". Example: RU12.",
        "misc.wbs_code_hint": "Format: function/subfunction/project.extension. Example: IT/Infr/DC.DCT3srv (the extension is optional).",
        "misc.wbs_move_warning": "Budget lines reference this WBS ({linked}). Changing the cost centre may change the legal entity, and with it the full WBS.",
        "misc.full_wbs_note": "The register is computed from the legal entity code and the WBS code. Records are created and edited in the WBS handbook.",
        "misc.wbs_meta": "Full WBS: {full} · legal entity: {le} · cost centre: {cc}",
        "misc.no_wbs": "not set",
        "misc.budget_wbs_missing": "This budget has no WBS — pick one when editing it.",
        "empty.legal_entities": "No legal entities yet",
        "empty.cost_centers": "No cost centres yet",
        "empty.wbs": "No WBS yet",
        "empty.cost_elements": "No cost elements yet",
        "empty.full_wbs": "The register is empty",
        "empty.wbs_ce": "No cost elements linked",
        "empty.budgets": "No budget lines",
        "filter.legal_entity": "Legal entity",
        "filter.cost_center": "Cost centre",
        "filter.all": "All",
        "flash.le_created": "Legal entity created",
        "flash.le_updated": "Legal entity updated",
        "flash.le_deleted": "Legal entity deleted",
        "flash.cc_created": "Cost centre created",
        "flash.cc_updated": "Cost centre updated",
        "flash.cc_deleted": "Cost centre deleted",
        "flash.wbs_created": "WBS created",
        "flash.wbs_updated": "WBS updated",
        "flash.wbs_deleted": "WBS deleted",
        "flash.ce_created": "Cost element created",
        "flash.ce_updated": "Cost element updated",
        "flash.ce_deleted": "Cost element deleted",
        "field.entity_code": "legal entity code",
        "field.wbs_code": "WBS code",
        "error.entity_code_format": "Legal entity code: letters and digits, 2–16 characters, no \"/\"",
        "error.ref_code_format": "Code: letters, digits, dot, hyphen or underscore, up to 32 characters, no \"/\"",
        "error.wbs_code_format": "The WBS code must look like function/subfunction/project.extension, for example IT/Infr/DC.DCT3srv",
        "error.le_not_found": "Legal entity not found",
        "error.cc_not_found": "Cost centre not found",
        "error.wbs_not_found": "WBS not found",
        "error.ce_not_found": "Cost element not found",
        "error.cannot_delete_le_linked": "Cannot delete the legal entity: it has cost centres",
        "error.cannot_delete_cc_linked": "Cannot delete the cost centre: it has WBS",
        "error.cannot_delete_wbs_linked": "Cannot delete the WBS: budget lines reference it",
        "error.cannot_delete_ce_linked": "Cannot delete the cost element: it is in use",
        "error.ce_not_in_wbs": "The cost element is not linked to the selected WBS",
        "error.wbs_required": "Select a WBS",
    },
}
for _lang, _msgs in _HANDBOOK_TRANSLATIONS.items():
    TRANSLATIONS.setdefault(_lang, {}).update(_msgs)


def normalize_lang(value):
    """Return `value` if it is a supported language code, else None."""
    value = (value or "").strip().lower()
    return value if value in LANGUAGES else None


def t(lang, key, **kwargs):
    """Look up a localized message.

    Falls back to the default language and finally to the key itself if a
    string is missing, so a forgotten key is visible but never crashes.
    `kwargs` fill {placeholders} via str.format().
    """
    catalog = TRANSLATIONS.get(lang) or TRANSLATIONS[DEFAULT_LANG]
    text = catalog.get(key)
    if text is None:
        text = TRANSLATIONS[DEFAULT_LANG].get(key, key)
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return text


@contextlib.contextmanager
def db(write=False):
    # write=True opens a BEGIN IMMEDIATE transaction so that a read-check
    # followed by a write is atomic against other writers. Without it two
    # concurrent requests could both pass an "available budget" check and
    # both commit, overspending the budget (TOCTOU race).
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.execute("COMMIT")
    except BaseException:
        if write:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def ensure_column(conn, table, column, ddl):
    """Add a column to an existing table if it is missing.

    init_db() only ever runs CREATE TABLE IF NOT EXISTS, which upgrades a
    database by adding whole tables but never touches a table that already
    exists. Columns added to an existing table therefore need this.
    """
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS budget_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                fiscal_year INTEGER NOT NULL,
                holder_name TEXT NOT NULL,
                holder_email TEXT,
                -- Legacy free-text columns, superseded by wbs_id/cost_element_id
                -- (added further down by ensure_column). Kept as a mirror of the
                -- resolved codes so external readers of the table keep working;
                -- the UI reads and writes the reference columns only.
                cost_center TEXT,
                wbs TEXT,
                cost_element TEXT,
                currency TEXT NOT NULL DEFAULT 'EUR',
                initial_approved_cents INTEGER NOT NULL CHECK(initial_approved_cents >= 0),
                initial_released_cents INTEGER NOT NULL CHECK(initial_released_cents >= 0),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS budget_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_type TEXT NOT NULL,
                source_budget_id INTEGER,
                target_budget_id INTEGER,
                amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                approved_delta_source INTEGER NOT NULL DEFAULT 0,
                released_delta_source INTEGER NOT NULL DEFAULT 0,
                approved_delta_target INTEGER NOT NULL DEFAULT 0,
                released_delta_target INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(source_budget_id) REFERENCES budget_lines(id),
                FOREIGN KEY(target_budget_id) REFERENCES budget_lines(id)
            );

            CREATE TABLE IF NOT EXISTS purchase_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT NOT NULL UNIQUE,
                budget_id INTEGER NOT NULL,
                vendor TEXT NOT NULL,
                description TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                status TEXT NOT NULL CHECK(status IN ('DRAFT','APPROVED','CLOSED','CANCELLED')),
                created_at TEXT NOT NULL,
                FOREIGN KEY(budget_id) REFERENCES budget_lines(id)
            );

            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                budget_id INTEGER NOT NULL,
                po_id INTEGER,
                expense_date TEXT NOT NULL,
                invoice_no TEXT,
                description TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                created_at TEXT NOT NULL,
                FOREIGN KEY(budget_id) REFERENCES budget_lines(id),
                FOREIGN KEY(po_id) REFERENCES purchase_orders(id)
            );

            CREATE TABLE IF NOT EXISTS budget_monthly_allocations (
                budget_id INTEGER NOT NULL,
                month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
                allocated_cents INTEGER NOT NULL DEFAULT 0 CHECK(allocated_cents >= 0),
                PRIMARY KEY(budget_id, month),
                FOREIGN KEY(budget_id) REFERENCES budget_lines(id)
            );

            CREATE TABLE IF NOT EXISTS currencies (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                rate_micro INTEGER,   -- rate to RUB per 1 unit, scaled x1e6; NULL = not fetched yet
                is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0,1)),
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- Reference data. The chain is legal entity -> cost centre -> WBS,
            -- with cost elements attached to a WBS many-to-many. Every code is
            -- unique inside its own dictionary, so the full WBS
            -- ("RU12/IT/Infr/DC.DCT3srv") is unique by construction and never
            -- needs a stored column of its own -- see full_wbs()/wbs_query().
            CREATE TABLE IF NOT EXISTS legal_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cost_centers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                legal_entity_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(legal_entity_id) REFERENCES legal_entities(id)
            );

            CREATE TABLE IF NOT EXISTS wbs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                cost_center_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(cost_center_id) REFERENCES cost_centers(id)
            );

            CREATE TABLE IF NOT EXISTS cost_elements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS wbs_cost_elements (
                wbs_id INTEGER NOT NULL,
                cost_element_id INTEGER NOT NULL,
                PRIMARY KEY(wbs_id, cost_element_id),
                FOREIGN KEY(wbs_id) REFERENCES wbs(id),
                FOREIGN KEY(cost_element_id) REFERENCES cost_elements(id)
            );

            CREATE INDEX IF NOT EXISTS idx_cost_centers_le ON cost_centers(legal_entity_id);
            CREATE INDEX IF NOT EXISTS idx_wbs_cc ON wbs(cost_center_id);
            CREATE INDEX IF NOT EXISTS idx_wbs_ce_element ON wbs_cost_elements(cost_element_id);
            """
        )
        # budget_lines predates the dictionaries, so its reference columns are
        # added in place. Nullable on purpose: rows from an older release that
        # carry unrecognised free text keep working and ask for a WBS the next
        # time they are edited.
        ensure_column(conn, "budget_lines", "wbs_id", "INTEGER REFERENCES wbs(id)")
        ensure_column(conn, "budget_lines", "cost_element_id", "INTEGER REFERENCES cost_elements(id)")
        conn.execute(
            """UPDATE budget_lines SET wbs_id=(SELECT w.id FROM wbs w WHERE w.code=budget_lines.wbs)
               WHERE wbs_id IS NULL AND COALESCE(wbs,'')<>''"""
        )
        conn.execute(
            """UPDATE budget_lines SET cost_element_id=(SELECT c.id FROM cost_elements c WHERE c.code=budget_lines.cost_element)
               WHERE cost_element_id IS NULL AND COALESCE(cost_element,'')<>''"""
        )
        # Currency catalog and app settings are core configuration and are
        # seeded regardless of SEED_DEMO. INSERT OR IGNORE keeps it idempotent
        # and never clobbers a rate the operator has already refreshed. RUB is
        # the CBR base: its rate is fixed at 1.0 (1_000_000 micro) and it stays
        # active so it can always serve as the default display currency.
        conn.executemany(
            "INSERT OR IGNORE INTO currencies(code,name,rate_micro,is_active) VALUES(?,?,?,?)",
            [
                ("RUB", "Российский рубль", 1_000_000, 1),
                ("USD", "Доллар США", None, 1),
                ("EUR", "Евро", None, 1),
                ("GBP", "Фунт стерлингов", None, 0),
                ("CNY", "Китайский юань", None, 0),
                ("KZT", "Казахстанский тенге", None, 0),
            ],
        )
        conn.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('base_currency','RUB')")
        count = conn.execute("SELECT COUNT(*) FROM budget_lines").fetchone()[0]
    if not (SEED_DEMO and count == 0):
        return
    with db(write=True) as conn:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        # Demo reference chain: legal entity -> cost centre -> WBS, plus one
        # cost element, so the seeded budget below points at real dictionary
        # rows and the full WBS reads RU12/IT/Infr/DC.DCT3srv.
        conn.execute("INSERT INTO legal_entities(code,name,created_at) VALUES(?,?,?)",
                     ("RU12", "Example Holding LLC", now))
        le_id = conn.execute("SELECT id FROM legal_entities WHERE code='RU12'").fetchone()[0]
        conn.execute("INSERT INTO cost_centers(code,name,legal_entity_id,created_at) VALUES(?,?,?,?)",
                     ("RU12-IT", "IT department", le_id, now))
        cc_id = conn.execute("SELECT id FROM cost_centers WHERE code='RU12-IT'").fetchone()[0]
        conn.execute("INSERT INTO wbs(code,name,cost_center_id,created_at) VALUES(?,?,?,?)",
                     ("IT/Infr/DC.DCT3srv", "Data centre, tier-3 servers", cc_id, now))
        wbs_id = conn.execute("SELECT id FROM wbs WHERE code='IT/Infr/DC.DCT3srv'").fetchone()[0]
        conn.execute("INSERT INTO cost_elements(code,name,created_at) VALUES(?,?,?)",
                     ("6100100", "IT services", now))
        ce_id = conn.execute("SELECT id FROM cost_elements WHERE code='6100100'").fetchone()[0]
        conn.execute("INSERT INTO wbs_cost_elements(wbs_id,cost_element_id) VALUES(?,?)", (wbs_id, ce_id))
        conn.execute(
            """INSERT INTO budget_lines
            (code,name,fiscal_year,holder_name,holder_email,cost_center,wbs,cost_element,currency,
             initial_approved_cents,initial_released_cents,created_at,wbs_id,cost_element_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("IT-OPS-2026", "IT Operations", 2026, "Budget Holder", "holder@example.com",
             "RU12-IT", "IT/Infr/DC.DCT3srv", "6100100", "EUR", 10000000, 10000000, now,
             wbs_id, ce_id),
        )
        budget_id = conn.execute("SELECT id FROM budget_lines WHERE code='IT-OPS-2026'").fetchone()[0]
        conn.execute(
            """INSERT INTO purchase_orders
            (number,budget_id,vendor,description,amount_cents,status,created_at)
            VALUES (?,?,?,?,?,?,?)""",
            ("PO-2026-0001", budget_id, "Example Vendor", "Infrastructure support, limit PO", 2500000, "APPROVED", now),
        )
        po_id = conn.execute("SELECT id FROM purchase_orders WHERE number='PO-2026-0001'").fetchone()[0]
        conn.execute(
            """INSERT INTO expenses
            (budget_id,po_id,expense_date,invoice_no,description,amount_cents,created_at)
            VALUES (?,?,?,?,?,?,?)""",
            (budget_id, po_id, date.today().isoformat(), "INV-DEMO-001", "Monthly support services", 700000, now),
        )
        # Monthly plan: spread released evenly, then shrink the current month
        # below the demo expense (moving the surplus to another month) so the
        # seeded app immediately shows one over-plan month. Total still equals
        # the released budget.
        values = spread_evenly(10000000)
        cur = date.today().month
        dst = 0 if cur == 12 else 11
        if values[cur - 1] > 500000:
            values[dst] += values[cur - 1] - 500000
            values[cur - 1] = 500000
        conn.executemany(
            "INSERT INTO budget_monthly_allocations(budget_id,month,allocated_cents) VALUES(?,?,?)",
            [(budget_id, i + 1, v) for i, v in enumerate(values)],
        )


def money_to_cents(value, lang=DEFAULT_LANG):
    text = (value or "").strip().replace(" ", "").replace("\u00a0", "")
    # Accept both "1,234.56" and "1.234,56": the rightmost separator is the
    # decimal point, the other one is a thousands separator and is dropped.
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        amount = Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError(t(lang, "error.bad_amount"))
    if amount <= 0:
        raise ValueError(t(lang, "error.amount_positive"))
    return int(amount * 100)


def money_to_cents_or_zero(value, lang=DEFAULT_LANG):
    """money_to_cents() for allocation fields, where blank or an explicit
    zero means "no budget this month" rather than an input error."""
    text = (value or "").strip()
    if not text or re.fullmatch(r"0+([.,]0+)?", text.replace(" ", "")):
        return 0
    return money_to_cents(value, lang)


def parse_int(value, label, lang=DEFAULT_LANG):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(t(lang, "error.bad_field", label=label))


def parse_date(value, lang=DEFAULT_LANG):
    try:
        return date.fromisoformat((value or "").strip()).isoformat()
    except ValueError:
        raise ValueError(t(lang, "error.bad_date"))


def require(data, field, label, lang=DEFAULT_LANG):
    value = (data.get(field) or "").strip()
    if not value:
        raise ValueError(t(lang, "error.field_required", label=label))
    return value


# Reference-data codes. None of them may contain "/" except the WBS code
# itself, which uses it as its own separator -- that is what keeps a full WBS
# ("RU12/IT/Infr/DC.DCT3srv") unambiguous when read back apart.
WBS_CODE_RE = re.compile(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?")
ENTITY_CODE_RE = re.compile(r"[A-Z0-9][A-Z0-9-]{1,15}")
REF_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")


def parse_wbs_code(data, field="code", lang=DEFAULT_LANG):
    """Validate a WBS code: function/subfunction/project[.extension]."""
    value = require(data, field, t(lang, "field.wbs_code"), lang)
    if not WBS_CODE_RE.fullmatch(value):
        raise ValueError(t(lang, "error.wbs_code_format"))
    return value


def parse_entity_code(data, field="code", lang=DEFAULT_LANG):
    """Validate a legal-entity code such as RU12. Case is normalised up."""
    value = require(data, field, t(lang, "field.entity_code"), lang).upper()
    if not ENTITY_CODE_RE.fullmatch(value):
        raise ValueError(t(lang, "error.entity_code_format"))
    return value


def parse_ref_code(data, field="code", lang=DEFAULT_LANG):
    """Validate a cost-centre or cost-element code."""
    value = require(data, field, t(lang, "field.code"), lang)
    if not REF_CODE_RE.fullmatch(value):
        raise ValueError(t(lang, "error.ref_code_format"))
    return value


def full_wbs(entity_code, wbs_code):
    """Compose the full WBS. Single place this string is built, so every list,
    detail page and API response spells it the same way."""
    return f"{entity_code}/{wbs_code}"


# One join for the whole entity chain. Selected by every page that needs to
# show a full WBS, so the three-table join is not spelled out over and over.
WBS_SELECT = """SELECT w.*, cc.id cc_id, cc.code cc_code, cc.name cc_name,
                       le.id le_id, le.code le_code, le.name le_name
                FROM wbs w
                JOIN cost_centers cc ON cc.id = w.cost_center_id
                JOIN legal_entities le ON le.id = cc.legal_entity_id"""


def wbs_query(conn, where="", params=(), order="le.code, w.code"):
    """Rows of `wbs` joined to their cost centre and legal entity."""
    sql = WBS_SELECT + (f" WHERE {where}" if where else "") + f" ORDER BY {order}"
    return conn.execute(sql, params).fetchall()


def wbs_row(conn, wbs_id):
    rows = wbs_query(conn, "w.id=?", (wbs_id,))
    return rows[0] if rows else None


def wbs_ce_ids(conn, wbs_id):
    """Cost-element ids linked to a WBS, as a set."""
    return {r["cost_element_id"] for r in conn.execute(
        "SELECT cost_element_id FROM wbs_cost_elements WHERE wbs_id=?", (wbs_id,))}


# URL prefix -> handler names. The four dictionaries share one list/detail/
# edit/create/update/delete shape, so do_GET/do_POST walk this table instead of
# repeating twelve near-identical routing branches.
HANDBOOKS = {
    "legal-entities": {"list": "le_page", "detail": "le_detail", "edit": "le_edit_page",
                       "create": "create_le", "update": "update_le", "remove": "delete_le"},
    "cost-centers": {"list": "cc_page", "detail": "cc_detail", "edit": "cc_edit_page",
                     "create": "create_cc", "update": "update_cc", "remove": "delete_cc"},
    "wbs": {"list": "wbs_page", "detail": "wbs_detail", "edit": "wbs_edit_page",
            "create": "create_wbs", "update": "update_wbs", "remove": "delete_wbs"},
    "cost-elements": {"list": "ce_page", "detail": "ce_detail", "edit": "ce_edit_page",
                      "create": "create_ce", "update": "update_ce", "remove": "delete_ce"},
}


def wbs_index(conn):
    """{wbs_id: joined row} — lets a budget list resolve its full WBS without
    running the three-table join once per line."""
    return {r["id"]: r for r in wbs_query(conn)}


def ce_index(conn):
    """{cost_element_id: code}."""
    return {r["id"]: r["code"] for r in conn.execute("SELECT id,code FROM cost_elements")}


def wbs_ce_labels(conn):
    """{wbs_id: "CE1, CE2"} for list pages, one query instead of one per row."""
    out = {}
    for r in conn.execute(
            """SELECT l.wbs_id, c.code FROM wbs_cost_elements l
               JOIN cost_elements c ON c.id=l.cost_element_id ORDER BY c.code"""):
        out.setdefault(r["wbs_id"], []).append(r["code"])
    return {k: ", ".join(v) for k, v in out.items()}


def fmt_money(cents, currency="EUR", lang=DEFAULT_LANG):
    value = Decimal(int(cents)) / 100
    s = f"{value:,.2f}"  # e.g. "1,234.56": comma thousands, dot decimal
    if lang == "ru":
        # Russian formatting: non-breaking-space thousands (U+00A0) and a
        # comma decimal separator, e.g. "1 234,56". English keeps the
        # "1,234.56" grouping produced above.
        s = s.replace(",", " ").replace(".", ",")
    return f"{s} {html.escape(currency)}"


def cents_to_input(cents):
    # Plain decimal string suitable for pre-filling an amount <input> so it
    # round-trips back through money_to_cents() on the next submit.
    return f"{Decimal(int(cents)) / 100:.2f}"


def budget_metrics(conn, budget_id):
    row = conn.execute("SELECT * FROM budget_lines WHERE id=?", (budget_id,)).fetchone()
    if not row:
        return None
    op = conn.execute(
        """SELECT
           COALESCE(SUM(CASE WHEN source_budget_id=? THEN approved_delta_source ELSE 0 END),0) +
           COALESCE(SUM(CASE WHEN target_budget_id=? THEN approved_delta_target ELSE 0 END),0) AS approved_delta,
           COALESCE(SUM(CASE WHEN source_budget_id=? THEN released_delta_source ELSE 0 END),0) +
           COALESCE(SUM(CASE WHEN target_budget_id=? THEN released_delta_target ELSE 0 END),0) AS released_delta
           FROM budget_operations""",
        (budget_id, budget_id, budget_id, budget_id),
    ).fetchone()
    actuals = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM expenses WHERE budget_id=?", (budget_id,)
    ).fetchone()[0]
    commitments = conn.execute(
        """SELECT COALESCE(SUM(MAX(po.amount_cents - COALESCE(e.spent,0),0)),0)
           FROM purchase_orders po
           LEFT JOIN (SELECT po_id, SUM(amount_cents) spent FROM expenses WHERE po_id IS NOT NULL GROUP BY po_id) e
             ON e.po_id=po.id
           WHERE po.budget_id=? AND po.status='APPROVED'""",
        (budget_id,),
    ).fetchone()[0]
    approved = row["initial_approved_cents"] + op["approved_delta"]
    released = row["initial_released_cents"] + op["released_delta"]
    available = released - actuals - commitments
    return {
        "row": row,
        "approved": approved,
        "released": released,
        "actuals": actuals,
        "commitments": commitments,
        "available": available,
    }


def all_budget_metrics(conn):
    rows = conn.execute("SELECT id FROM budget_lines ORDER BY fiscal_year DESC, code").fetchall()
    return [budget_metrics(conn, r["id"]) for r in rows]


def assert_budget_ok(conn, budget_id, lang=DEFAULT_LANG):
    """Re-check the core financial invariants for a budget after a mutation.

    Every update/delete path calls this from inside the write transaction, so
    that editing or removing a record can never leave a budget over-released
    (released > approved) or overspent (available < 0). Raising here rolls the
    enclosing transaction back. A missing budget_id is a no-op so callers can
    pass an optional target budget unconditionally.
    """
    if not budget_id:
        return
    m = budget_metrics(conn, budget_id)
    if not m:
        return
    code = m["row"]["code"]
    if m["released"] > m["approved"]:
        raise ValueError(t(lang, "error.released_exceeds_approved", code=code))
    if m["available"] < 0:
        raise ValueError(t(lang, "error.available_negative", code=code))


def spread_evenly(total_cents):
    """Split `total_cents` into 12 monthly amounts differing by at most one
    cent, with the remainder going to the earliest months. Sum is exact."""
    base, extra = divmod(int(total_cents), 12)
    return [base + 1 if i < extra else base for i in range(12)]


def monthly_metrics(conn, budget_id):
    """Per-month plan vs actuals for a budget line's fiscal year.

    Expenses are bucketed by expense_date (always YYYY-MM-DD, enforced by
    parse_date). Months are keyed to the line's fiscal_year, so changing the
    year on the line re-buckets actuals automatically. A line with no
    allocation rows has no monthly plan: has_plan is False and no month is
    flagged `over` (legacy annual-only behavior). Monthly control is soft —
    nothing here blocks postings; the hard limit stays in budget_metrics.
    """
    row = conn.execute("SELECT * FROM budget_lines WHERE id=?", (budget_id,)).fetchone()
    if not row:
        return None
    year = str(row["fiscal_year"])
    alloc = dict(conn.execute(
        "SELECT month, allocated_cents FROM budget_monthly_allocations WHERE budget_id=?",
        (budget_id,)).fetchall())
    actual = dict(conn.execute(
        """SELECT CAST(substr(expense_date,6,2) AS INTEGER) m, COALESCE(SUM(amount_cents),0)
           FROM expenses WHERE budget_id=? AND substr(expense_date,1,4)=? GROUP BY m""",
        (budget_id, year)).fetchall())
    out_of_year = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) FROM expenses WHERE budget_id=? AND substr(expense_date,1,4)<>?",
        (budget_id, year)).fetchone()[0]
    has_plan = bool(alloc)
    months = []
    for m in range(1, 13):
        allocated = alloc.get(m, 0)
        actuals = actual.get(m, 0)
        months.append({
            "month": m,
            "allocated": allocated,
            "actuals": actuals,
            "remaining": allocated - actuals,
            "over": has_plan and actuals > allocated,
        })
    return {
        "row": row,
        "has_plan": has_plan,
        "months": months,
        "allocated_total": sum(alloc.values()),
        "actuals_in_year": sum(actual.values()),
        "actuals_out_of_year": out_of_year,
    }


def month_overspent(conn, budget_id, expense_date):
    """True when the month containing `expense_date` is over its allocation
    (used for the soft-control warning flash after posting an expense)."""
    mm = monthly_metrics(conn, budget_id)
    if not mm or not mm["has_plan"]:
        return False
    if expense_date[:4] != str(mm["row"]["fiscal_year"]):
        return False
    return mm["months"][int(expense_date[5:7]) - 1]["over"]


def compute_operation_deltas(op, amount, source, target, lang=DEFAULT_LANG):
    """Validate a budget operation and return the (approved/released) deltas
    (source_approved, source_released, target_approved, target_released).

    `source` and `target` are budget_metrics() dicts; `target` is None for
    non-transfer operations. Raises ValueError on any rule violation. Kept
    free of I/O so the business rules can be unit-tested in isolation.
    """
    sa = sr = ta = tr = 0
    if op == "SUPPLEMENT":
        sa = sr = amount
    elif op == "REDUCTION":
        if source["released"] - amount < source["actuals"] + source["commitments"]:
            raise ValueError(t(lang, "error.reduction_negative"))
        sa = sr = -amount
    elif op == "RELEASE":
        if source["released"] + amount > source["approved"]:
            raise ValueError(t(lang, "error.release_exceeds"))
        sr = amount
    elif op == "RETURN":
        if source["released"] - amount < source["actuals"] + source["commitments"]:
            raise ValueError(t(lang, "error.return_used"))
        sr = -amount
    elif op in {"TRANSFER", "CARRY_FORWARD"}:
        if not target:
            raise ValueError(t(lang, "error.target_not_found"))
        if source["row"]["currency"] != target["row"]["currency"]:
            raise ValueError(t(lang, "error.currency_mismatch"))
        if source["released"] - amount < source["actuals"] + source["commitments"]:
            raise ValueError(t(lang, "error.insufficient_transfer"))
        if op == "CARRY_FORWARD" and target["row"]["fiscal_year"] <= source["row"]["fiscal_year"]:
            raise ValueError(t(lang, "error.carry_forward_year"))
        sa = sr = -amount
        ta = tr = amount
    else:
        raise ValueError(t(lang, "error.unknown_operation"))
    return sa, sr, ta, tr


def esc(value):
    return html.escape(str(value or ""))


# --------------------------------------------------------------------------- #
# Multi-currency: settings, CBR exchange rates and conversion                  #
# --------------------------------------------------------------------------- #
# Rates are stored relative to RUB (the CBR base) as integers scaled by 1e6:
# rate_micro = round(rate_to_RUB_per_unit * 1_000_000). RUB itself is 1.0.
CBR_URL = env_str("CBR_URL", "https://www.cbr.ru/scripts/XML_daily.asp")
CBR_TIMEOUT = env_int("CBR_TIMEOUT", 10, minimum=1)
RUB_MICRO = 1_000_000


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO app_settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def load_rates(conn):
    """Map currency code -> rate_micro for every currency that has a rate.
    RUB is always present at 1.0 so it can serve as the conversion pivot."""
    rates = {r["code"]: r["rate_micro"] for r in
             conn.execute("SELECT code, rate_micro FROM currencies WHERE rate_micro IS NOT NULL")}
    rates.setdefault("RUB", RUB_MICRO)
    return rates


def active_currencies(conn):
    """Active currency rows (code, name, rate_micro, updated_at), code-sorted."""
    return conn.execute(
        "SELECT code, name, rate_micro, updated_at FROM currencies WHERE is_active=1 ORDER BY code"
    ).fetchall()


def convert_cents(cents, from_ccy, to_ccy, rates):
    """Convert integer-cents between two currencies via their rate-to-RUB.
    Returns int cents in `to_ccy`, or None if either side has no known rate.
    Decimal throughout; the 1e6 rate scale cancels, so no rounding of rates."""
    if from_ccy == to_ccy:
        return int(cents)
    rate_from = rates.get(from_ccy)
    rate_to = rates.get(to_ccy)
    if not rate_from or not rate_to:
        return None
    result = (Decimal(int(cents)) * Decimal(rate_from) / Decimal(rate_to)).quantize(
        Decimal(1), rounding=ROUND_HALF_UP)
    return int(result)


def parse_cbr_rates(xml_text):
    """Parse a CBR XML_daily document (already decoded to str) into
    {CharCode: (name, rate_micro)}. VunitRate is the value of one unit in RUB
    (decimal comma). Pure function (no network) so it is unit-testable."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_text)
    out = {}
    for valute in root.findall("Valute"):
        code = (valute.findtext("CharCode") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", code):
            continue
        name = (valute.findtext("Name") or code).strip()

        def _dec(text):
            return Decimal((text or "0").replace(" ", "").replace("\u00a0", "").replace(",", "."))

        vunit = valute.findtext("VunitRate")
        if vunit:
            per_unit = _dec(vunit)                       # already per 1 unit
        else:
            nominal = _dec(valute.findtext("Nominal") or "1")
            per_unit = _dec(valute.findtext("Value")) / nominal if nominal else Decimal(0)
        if per_unit <= 0:
            continue
        out[code] = (name, int((per_unit * RUB_MICRO).quantize(Decimal(1), rounding=ROUND_HALF_UP)))
    return out


def fetch_cbr_rates(url=None, timeout=None):
    """Fetch and parse today's CBR rates. Network is isolated from parsing so
    tests can stub this out. Raises ValueError on any network/parse failure."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url or CBR_URL, headers={"User-Agent": "BudgetControl/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or CBR_TIMEOUT) as resp:
            raw = resp.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ValueError(str(exc))
    try:
        text = raw.decode("windows-1251")               # CBR serves cp1251
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    rates = parse_cbr_rates(text)
    if not rates:
        raise ValueError("empty CBR response")
    return rates


def refresh_rates(conn, fetch=fetch_cbr_rates):
    """Fetch CBR rates and upsert them into `currencies`. New codes are inserted
    inactive; existing ones get name/rate/updated_at refreshed. RUB is never
    touched (fixed 1.0 base). Returns the number of currencies written. `fetch`
    is injectable so tests can supply a fixture instead of hitting the network."""
    rates = fetch()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    count = 0
    for code, (name, rate_micro) in rates.items():
        if code == "RUB":
            continue
        conn.execute(
            "INSERT INTO currencies(code,name,rate_micro,is_active,updated_at) VALUES(?,?,?,0,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, rate_micro=excluded.rate_micro, "
            "updated_at=excluded.updated_at",
            (code, name, rate_micro, now),
        )
        count += 1
    set_setting(conn, "rates_updated_at", now)
    return count


CSS = r"""
:root { --bg:#f4f6f8; --panel:#fff; --text:#18212b; --muted:#64748b; --line:#dbe2ea; --accent:#2457d6; --good:#137a4f; --warn:#9a6700; --bad:#b42318; }
*{box-sizing:border-box} body{margin:0;font-family:Inter,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--text)}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
header{background:#111827;color:#fff;padding:0 24px}.top{max-width:1280px;margin:auto;display:flex;align-items:center;justify-content:space-between;min-height:62px}
.brand{font-weight:700}.nav{display:flex;gap:18px;flex-wrap:wrap}.nav a{color:#dbeafe}.container{max-width:1280px;margin:24px auto;padding:0 18px}
.grid{display:grid;gap:16px}.cards{grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px;box-shadow:0 1px 2px rgba(0,0,0,.03)}
.metric{font-size:25px;font-weight:700;margin-top:8px}.label{color:var(--muted);font-size:13px}.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}
h1{font-size:26px;margin:0 0 18px}h2{font-size:19px;margin:0 0 14px}h3{font-size:16px;margin:0 0 10px}
table{width:100%;border-collapse:collapse;background:#fff}th,td{text-align:left;border-bottom:1px solid var(--line);padding:11px 10px;vertical-align:top}th{font-size:12px;text-transform:uppercase;color:var(--muted);background:#f8fafc}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px}.toolbar{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
form.inline{display:inline}.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.full{grid-column:1/-1}
label{display:block;font-size:13px;color:#475569;margin-bottom:5px}input,select,textarea{width:100%;padding:10px;border:1px solid #cbd5e1;border-radius:7px;background:#fff;font:inherit}textarea{min-height:76px;resize:vertical}
button,.button{display:inline-block;border:0;border-radius:7px;padding:10px 14px;background:var(--accent);color:#fff;font-weight:600;cursor:pointer}.button.secondary,button.secondary{background:#475569}.button.danger,button.danger{background:var(--bad)}
.badge{display:inline-block;padding:4px 8px;border-radius:999px;font-size:12px;font-weight:700;background:#e2e8f0}.badge.APPROVED{background:#dcfce7;color:#166534}.badge.DRAFT{background:#fef3c7;color:#92400e}.badge.CLOSED{background:#e0e7ff;color:#3730a3}.badge.CANCELLED{background:#fee2e2;color:#991b1b}
.badge.OK{background:#dcfce7;color:#166534}.badge.OVER{background:#fee2e2;color:#991b1b}tr.over td{background:#fef2f2}
.flash{padding:12px 14px;border-radius:8px;margin-bottom:16px;background:#dbeafe;color:#1e3a8a}.flash.error{background:#fee2e2;color:#991b1b}.flash.warn{background:#fef3c7;color:#92400e}.muted{color:var(--muted)}.small{font-size:12px}.split{display:grid;grid-template-columns:2fr 1fr;gap:16px}@media(max-width:850px){.split{grid-template-columns:1fr}.nav{gap:10px}.top{align-items:flex-start;padding:14px 0;flex-direction:column}}
.progress{height:9px;background:#e2e8f0;border-radius:999px;overflow:hidden}.progress>span{display:block;height:100%;background:var(--accent)}.footer{color:var(--muted);font-size:12px;margin:26px 0}
.langsw{color:#93c5fd;margin-left:10px;font-size:13px}.langsw.active{color:#fff;font-weight:700}
.ccysw{display:flex;gap:6px;align-items:center;flex-wrap:wrap}.ccysw .langsw{color:var(--accent);margin-left:0}.ccysw .langsw.active{color:var(--text);font-weight:700}
"""


class AppHandler(BaseHTTPRequestHandler):
    server_version = "BudgetControl/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} - {fmt % args}")

    def _authorized(self):
        if not APP_USER:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, password = decoded.split(":", 1)
            return hmac.compare_digest(user, APP_USER) and hmac.compare_digest(password, APP_PASSWORD)
        except Exception:
            return False

    def _require_auth(self):
        if self._authorized():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Budget Control"')
        self.end_headers()
        return False

    def cookie_secure_attr(self):
        """`; Secure` when cookies should be HTTPS-only, else empty.

        Hosting platforms terminate TLS in front of the container, so the
        connection here is plain HTTP and only the forwarded-scheme header can
        tell us what the browser actually used.
        """
        if COOKIE_SECURE in ("1", "true", "yes", "on"):
            return "; Secure"
        if COOKIE_SECURE in ("0", "false", "no", "off"):
            return ""
        if not FORWARDED_PROTO_HEADER:
            return ""
        # A chain of proxies appends to the header; the client-facing hop is first.
        proto = self.headers.get(FORWARDED_PROTO_HEADER, "").split(",")[0].strip().lower()
        return "; Secure" if proto == "https" else ""

    def csrf_token(self):
        cached = getattr(self, "_csrf_cache", None)
        if cached:
            return cached
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            key, _, value = part.strip().partition("=")
            if key == "csrf_token" and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value or ""):
                self._csrf_cache = (value, False)
                return self._csrf_cache
        self._csrf_cache = (secrets.token_urlsafe(32), True)
        return self._csrf_cache

    def parse_post(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError(self.t("error.request_too_large"))
        body = self.rfile.read(length).decode("utf-8")
        data = {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}
        token, _ = self.csrf_token()
        if not hmac.compare_digest(data.get("csrf_token", ""), token):
            raise ValueError(self.t("error.csrf"))
        return data

    def redirect(self, path, message=None, error=False, kind=None):
        if message:
            sep = "&" if "?" in path else "?"
            path += sep + urlencode({"msg": message, "kind": kind or ("error" if error else "ok")})
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def send_html(self, content, status=200):
        token, is_new = self.csrf_token()
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        secure = self.cookie_secure_attr()
        if is_new:
            self.send_header("Set-Cookie", f"csrf_token={token}; Path=/; SameSite=Strict; HttpOnly{secure}")
        pending_lang = getattr(self, "_set_lang_cookie", None)
        if pending_lang:
            self.send_header("Set-Cookie", f"{LANG_COOKIE}={pending_lang}; Path=/; SameSite=Lax{secure}")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def page(self, title, body):
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        flash = ""
        if q.get("msg"):
            kind = q.get("kind", [""])[0]
            cls = {"error": "flash error", "warn": "flash warn"}.get(kind, "flash")
            flash = f'<div class="{cls}">{esc(q["msg"][0])}</div>'
        nav = (f'<a href="/">{esc(self.t("nav.overview"))}</a>'
               f'<a href="/budgets">{esc(self.t("nav.budgets"))}</a>'
               f'<a href="/pos">{esc(self.t("nav.pos"))}</a>'
               f'<a href="/expenses">{esc(self.t("nav.expenses"))}</a>'
               f'<a href="/operations">{esc(self.t("nav.operations"))}</a>'
               f'<a href="/handbooks">{esc(self.t("nav.handbooks"))}</a>'
               f'<a href="/settings">{esc(self.t("nav.settings"))}</a>')
        return f"""<!doctype html><html lang="{esc(self.lang)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{esc(title)} — {APP_NAME}</title><link rel="stylesheet" href="/static/style.css"></head><body>
        <header><div class="top"><div class="brand">{APP_NAME}</div><nav class="nav">
        {nav}{self.lang_switch_links()}</nav></div></header>
        <main class="container">{flash}{body}<div class="footer">{esc(self.t("misc.footer"))}</div></main></body></html>"""

    def csrf_input(self):
        token, _ = self.csrf_token()
        return f'<input type="hidden" name="csrf_token" value="{esc(token)}">'

    # ------------------------------------------------------------------ #
    # i18n helpers (bound to the language resolved for this request)      #
    # ------------------------------------------------------------------ #
    def resolve_lang(self):
        """Pick the UI language for this request. Priority: an explicit
        ?lang= switch (also remembered in a cookie), then the cookie, then the
        browser's Accept-Language header, then DEFAULT_LANG."""
        self._set_lang_cookie = None
        parsed = urlparse(self.path)
        requested = normalize_lang(parse_qs(parsed.query).get("lang", [""])[0])
        if requested:
            self._set_lang_cookie = requested
            return requested
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            key, _, value = part.strip().partition("=")
            if key == LANG_COOKIE:
                got = normalize_lang(value)
                if got:
                    return got
        for chunk in self.headers.get("Accept-Language", "").split(","):
            got = normalize_lang(chunk.split(";")[0].strip()[:2])
            if got:
                return got
        return DEFAULT_LANG

    def t(self, key, **kwargs):
        """Translate `key` into the current request language."""
        return t(getattr(self, "lang", DEFAULT_LANG), key, **kwargs)

    def money(self, cents, currency="EUR"):
        """fmt_money() bound to the current request language."""
        return fmt_money(cents, currency, getattr(self, "lang", DEFAULT_LANG))

    def lang_switch_links(self):
        """Render the RU/EN switcher, preserving the current path and query
        (minus lang/flash params) so the visitor stays on the same page."""
        parsed = urlparse(self.path)
        params = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        for drop in ("lang", "msg", "kind"):
            params.pop(drop, None)
        out = []
        for code in LANGUAGES:
            query = urlencode({**params, "lang": code})
            href = parsed.path + ("?" + query if query else "")
            if code == getattr(self, "lang", DEFAULT_LANG):
                out.append(f'<span class="langsw active">{esc(code.upper())}</span>')
            else:
                out.append(f'<a class="langsw" href="{esc(href)}">{esc(code.upper())}</a>')
        return "".join(out)

    def op_type_options(self, selected=None):
        """<option> list for the six budget operation types, localized."""
        pairs = (("SUPPLEMENT", "opt.op_supplement"), ("REDUCTION", "opt.op_reduction"),
                 ("RELEASE", "opt.op_release"), ("RETURN", "opt.op_return"),
                 ("TRANSFER", "opt.op_transfer"), ("CARRY_FORWARD", "opt.op_carry"))
        return "".join(
            f'<option value="{code}"{" selected" if code == selected else ""}>{esc(self.t(key))}</option>'
            for code, key in pairs)

    # ------------------------------------------------------------------ #
    # Currency display helpers (bound to the request's display currency) #
    # ------------------------------------------------------------------ #
    def ensure_display_context(self):
        """Resolve, once per request, the exchange rates, active-currency list,
        base currency and the effective display currency (a ?ccy= override that
        names an active currency, else the base). Cached on the handler and
        reset each request in do_GET so keep-alive connections stay correct."""
        if getattr(self, "_disp_loaded", False):
            return
        self._disp_loaded = True
        with db() as conn:
            self.rates = load_rates(conn)
            self.base_ccy = get_setting(conn, "base_currency", "RUB")
            self.active_ccy = [row["code"] for row in active_currencies(conn)]
        if self.base_ccy not in self.active_ccy:
            self.active_ccy.append(self.base_ccy)
        requested = parse_qs(urlparse(self.path).query).get("ccy", [""])[0].strip().upper()
        self.display_ccy = requested if requested in self.active_ccy else self.base_ccy

    def money_disp(self, cents, native_ccy):
        """Format `cents` (held in native_ccy) for display. When the display
        currency differs, the converted amount leads and the native amount is
        shown muted in parentheses; if no rate exists the native amount is shown
        with a 'no rate' note so a value is never silently dropped."""
        self.ensure_display_context()
        disp = self.display_ccy
        if not native_ccy or native_ccy == disp:
            return fmt_money(cents, native_ccy or disp, self.lang)
        converted = convert_cents(cents, native_ccy, disp, self.rates)
        native = fmt_money(cents, native_ccy, self.lang)
        if converted is None:
            return f'{native} <span class="muted small">({esc(self.t("cur.no_rate"))})</span>'
        return f'{fmt_money(converted, disp, self.lang)} <span class="muted small">({native})</span>'

    def currency_switcher(self):
        """Toolbar widget linking to the current page in each active currency
        (?ccy=CODE), current one highlighted. Preserves other query params so
        filters/paging survive the switch. Hidden when only one currency."""
        self.ensure_display_context()
        if len(self.active_ccy) < 2:
            return ""
        parsed = urlparse(self.path)
        params = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        for drop in ("ccy", "msg", "kind"):
            params.pop(drop, None)
        links = []
        for code in self.active_ccy:
            query = urlencode({**params, "ccy": code})
            href = parsed.path + ("?" + query if query else "")
            if code == self.display_ccy:
                links.append(f'<span class="langsw active">{esc(code)}</span>')
            else:
                links.append(f'<a class="langsw" href="{esc(href)}">{esc(code)}</a>')
        return (f'<div class="ccysw"><span class="muted small">{esc(self.t("cur.display"))}:</span>'
                f'{"".join(links)}</div>')

    def currency_options(self, selected, include=None):
        """<option> list of active currency codes for a budget's currency
        selector. `include` keeps a budget's current (possibly deactivated)
        currency selectable so editing it round-trips."""
        self.ensure_display_context()
        codes = list(self.active_ccy)
        if include and include not in codes:
            codes.append(include)
        return "".join(
            f'<option value="{esc(c)}"{" selected" if c == selected else ""}>{esc(c)}</option>'
            for c in codes)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        # Answered before the auth gate on purpose: the container HEALTHCHECK
        # probes /healthz without credentials, so leaving it behind Basic Auth
        # would flip the container to unhealthy as soon as APP_USER is set.
        # It exposes nothing but liveness.
        if path == "/healthz":
            self.send_json({"status": "ok"}); return
        if not self._require_auth():
            return
        self.lang = self.resolve_lang()
        self._disp_loaded = False
        if path == "/static/style.css":
            body = CSS.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if path == "/api/summary":
            return self.api_summary()
        self.ensure_display_context()
        if path == "/settings":
            return self.settings_page()
        if path == "/":
            return self.dashboard()
        if path == "/budgets":
            return self.budgets_page()
        m = re.fullmatch(r"/budgets/(\d+)", path)
        if m:
            return self.budget_detail(int(m.group(1)))
        m = re.fullmatch(r"/budgets/(\d+)/edit", path)
        if m:
            return self.budget_edit_page(int(m.group(1)))
        if path == "/pos":
            return self.pos_page()
        m = re.fullmatch(r"/pos/(\d+)", path)
        if m:
            return self.po_detail(int(m.group(1)))
        if path == "/expenses":
            return self.expenses_page()
        m = re.fullmatch(r"/expenses/(\d+)", path)
        if m:
            return self.expense_detail(int(m.group(1)))
        if path == "/operations":
            return self.operations_page()
        m = re.fullmatch(r"/operations/(\d+)", path)
        if m:
            return self.operation_detail(int(m.group(1)))
        if path == "/handbooks":
            return self.handbooks_page()
        if path == "/full-wbs":
            return self.full_wbs_page(parse_qs(parsed.query))
        # Reference dictionaries. All four follow the same list / detail / edit
        # shape, so one table drives the routing instead of twelve branches.
        for prefix, spec in HANDBOOKS.items():
            if path == f"/{prefix}":
                return getattr(self, spec["list"])()
            m = re.fullmatch(rf"/{prefix}/(\d+)", path)
            if m:
                return getattr(self, spec["detail"])(int(m.group(1)))
            m = re.fullmatch(rf"/{prefix}/(\d+)/edit", path)
            if m:
                return getattr(self, spec["edit"])(int(m.group(1)))
        self.send_html(self.page(self.t("title.not_found"),
                                  f'<h1>404</h1><p>{esc(self.t("misc.page_not_found"))}</p>'), 404)

    def do_POST(self):
        if not self._require_auth():
            return
        self.lang = self.resolve_lang()
        path = urlparse(self.path).path
        try:
            data = self.parse_post()
            if path == "/settings":
                return self.save_settings(data)
            if path == "/settings/refresh-rates":
                return self.refresh_rates_action(data)
            if path == "/budgets/new":
                return self.create_budget(data)
            m = re.fullmatch(r"/budgets/(\d+)/operation", path)
            if m:
                return self.create_operation(int(m.group(1)), data)
            m = re.fullmatch(r"/budgets/(\d+)/allocations", path)
            if m:
                return self.save_allocations(int(m.group(1)), data)
            m = re.fullmatch(r"/budgets/(\d+)/edit", path)
            if m:
                return self.update_budget(int(m.group(1)), data)
            m = re.fullmatch(r"/budgets/(\d+)/delete", path)
            if m:
                return self.delete_budget(int(m.group(1)), data)
            if path == "/pos/new":
                return self.create_po(data)
            m = re.fullmatch(r"/pos/(\d+)/status", path)
            if m:
                return self.change_po_status(int(m.group(1)), data)
            m = re.fullmatch(r"/pos/(\d+)/edit", path)
            if m:
                return self.update_po(int(m.group(1)), data)
            m = re.fullmatch(r"/pos/(\d+)/delete", path)
            if m:
                return self.delete_po(int(m.group(1)), data)
            if path == "/expenses/new":
                return self.create_expense(data)
            m = re.fullmatch(r"/expenses/(\d+)/edit", path)
            if m:
                return self.update_expense(int(m.group(1)), data)
            m = re.fullmatch(r"/expenses/(\d+)/delete", path)
            if m:
                return self.delete_expense(int(m.group(1)), data)
            m = re.fullmatch(r"/operations/(\d+)/edit", path)
            if m:
                return self.update_operation(int(m.group(1)), data)
            m = re.fullmatch(r"/operations/(\d+)/delete", path)
            if m:
                return self.delete_operation(int(m.group(1)), data)
            for prefix, spec in HANDBOOKS.items():
                if path == f"/{prefix}/new":
                    return getattr(self, spec["create"])(data)
                m = re.fullmatch(rf"/{prefix}/(\d+)/edit", path)
                if m:
                    return getattr(self, spec["update"])(int(m.group(1)), data)
                m = re.fullmatch(rf"/{prefix}/(\d+)/delete", path)
                if m:
                    return getattr(self, spec["remove"])(int(m.group(1)), data)
            self.redirect("/", self.t("error.unknown_action"), True)
        except (ValueError, sqlite3.IntegrityError) as exc:
            back = self.headers.get("Referer", "/")
            back_path = urlparse(back).path or "/"
            self.redirect(back_path, str(exc), True)
        except Exception as exc:
            print("ERROR", repr(exc))
            self.redirect("/", self.t("error.internal"), True)

    def dashboard(self):
        with db() as conn:
            metrics = all_budget_metrics(conn)
            recent = conn.execute(
                """SELECT e.*, b.code, b.currency, po.number po_number FROM expenses e
                   JOIN budget_lines b ON b.id=e.budget_id LEFT JOIN purchase_orders po ON po.id=e.po_id
                   ORDER BY e.id DESC LIMIT 8"""
            ).fetchall()
        # Budgets can be in different currencies, so totals are converted into
        # the display currency before summing. Budgets whose currency has no
        # rate are left out of the sum and reported so the number is honest.
        self.ensure_display_context()
        disp = self.display_ccy
        keys = ("approved", "released", "actuals", "commitments", "available")
        totals = {k: 0 for k in keys}
        missing = []
        for m in metrics:
            ccy = m["row"]["currency"]
            for k in keys:
                c = convert_cents(m[k], ccy, disp, self.rates)
                if c is None:
                    if ccy not in missing:
                        missing.append(ccy)
                else:
                    totals[k] += c
        warn = (f'<div class="flash error">{esc(self.t("misc.dashboard_no_rate", codes=", ".join(sorted(missing))))}</div>'
                if missing else "")
        rows = "".join(
            f"<tr><td>{esc(r['expense_date'])}</td><td><a href='/budgets/{r['budget_id']}'>{esc(r['code'])}</a></td>"
            f"<td>{esc(r['description'])}</td><td>{esc(r['po_number'] or self.t('misc.no_po'))}</td><td>{self.money_disp(r['amount_cents'],r['currency'])}</td></tr>"
            for r in recent
        ) or f"<tr><td colspan='5' class='muted'>{esc(self.t('empty.recent'))}</td></tr>"
        body = f"""<div class="toolbar"><h1>{esc(self.t('h1.dashboard'))}</h1>{self.currency_switcher()}</div>{warn}<div class="grid cards">
        <div class="card"><div class="label">{esc(self.t('metric.approved'))}</div><div class="metric">{fmt_money(totals['approved'],disp,self.lang)}</div></div>
        <div class="card"><div class="label">{esc(self.t('metric.released'))}</div><div class="metric">{fmt_money(totals['released'],disp,self.lang)}</div></div>
        <div class="card"><div class="label">{esc(self.t('metric.actuals'))}</div><div class="metric">{fmt_money(totals['actuals'],disp,self.lang)}</div></div>
        <div class="card"><div class="label">{esc(self.t('metric.commitments'))}</div><div class="metric">{fmt_money(totals['commitments'],disp,self.lang)}</div></div>
        <div class="card"><div class="label">{esc(self.t('metric.available'))}</div><div class="metric {'bad' if totals['available'] < 0 else 'good'}">{fmt_money(totals['available'],disp,self.lang)}</div></div>
        </div><br><div class="panel"><h2>{esc(self.t('h2.recent'))}</h2><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.date'))}</th><th>{esc(self.t('col.budget'))}</th><th>{esc(self.t('col.description'))}</th><th>{esc(self.t('col.po'))}</th><th>{esc(self.t('col.amount'))}</th></tr></thead><tbody>{rows}</tbody></table></div></div>"""
        self.send_html(self.page(self.t('nav.overview'), body))

    def budgets_page(self):
        with db() as conn:
            metrics = all_budget_metrics(conn)
            wbs_by_id = wbs_index(conn)
            ce_by_id = ce_index(conn)
            wbs_opts = self.wbs_options(conn)
        rows = ""
        for m in metrics:
            r = m["row"]
            w = wbs_by_id.get(r["wbs_id"])
            if w:
                cc_cell = f'<a href="/cost-centers/{w["cc_id"]}">{esc(w["cc_code"])}</a>'
                wbs_cell = f'<a href="/wbs/{w["id"]}">{esc(full_wbs(w["le_code"], w["code"]))}</a>'
            else:
                cc_cell = f'<span class="muted">{esc(self.t("misc.no_wbs"))}</span>'
                wbs_cell = f'<span class="muted">{esc(self.t("misc.no_wbs"))}</span>'
            ce_cell = esc(ce_by_id.get(r["cost_element_id"], "—"))
            usage = 0 if m["released"] <= 0 else min(100, max(0, round((m["actuals"] + m["commitments"]) * 100 / m["released"])))
            rows += f"""<tr><td><a href="/budgets/{r['id']}"><strong>{esc(r['code'])}</strong></a><div class="small muted">{esc(r['name'])}</div></td>
            <td>{r['fiscal_year']}</td><td>{esc(r['holder_name'])}</td><td>{cc_cell}</td><td>{wbs_cell}</td><td>{ce_cell}</td>
            <td>{self.money_disp(m['released'],r['currency'])}<div class="progress"><span style="width:{usage}%"></span></div></td>
            <td>{self.money_disp(m['actuals'],r['currency'])}</td><td>{self.money_disp(m['commitments'],r['currency'])}</td>
            <td class="{'bad' if m['available'] < 0 else 'good'}"><strong>{self.money_disp(m['available'],r['currency'])}</strong></td>
            <td><a class="button secondary" href="/budgets/{r['id']}/edit">{esc(self.t('btn.edit'))}</a></td></tr>"""
        body = f"""<div class="toolbar"><h1>{esc(self.t('nav.budgets'))}</h1>{self.currency_switcher()}</div><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.code'))}</th><th>{esc(self.t('col.year'))}</th><th>{esc(self.t('col.holder'))}</th><th>{esc(self.t('col.cost_center'))}</th><th>{esc(self.t('col.wbs'))}</th><th>{esc(self.t('col.ce'))}</th><th>{esc(self.t('col.released'))}</th><th>{esc(self.t('col.actuals'))}</th><th>{esc(self.t('col.commitments'))}</th><th>{esc(self.t('col.available'))}</th><th>{esc(self.t('col.actions'))}</th></tr></thead><tbody>{rows}</tbody></table></div>
        <br><div class="panel"><h2>{esc(self.t('h2.create_budget'))}</h2><form method="post" action="/budgets/new">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.code'))} *</label><input name="code" required placeholder="IT-OPS-2027"></div><div><label>{esc(self.t('label.name'))} *</label><input name="name" required></div>
        <div><label>{esc(self.t('label.fiscal_year'))} *</label><input type="number" name="fiscal_year" required value="{date.today().year}"></div><div><label>{esc(self.t('label.currency'))} *</label><select name="currency" required>{self.currency_options(self.base_ccy)}</select></div>
        <div><label>{esc(self.t('label.holder'))} *</label><input name="holder_name" required></div><div><label>{esc(self.t('label.email'))}</label><input type="email" name="holder_email"></div>
        <div class="full"><label>{esc(self.t('label.wbs_sel'))} *</label><select name="wbs_id" required>{wbs_opts}</select></div>
        <div><label>{esc(self.t('label.approved'))} *</label><input name="approved" required placeholder="100000.00"></div><div><label>{esc(self.t('label.released'))} *</label><input name="released" required placeholder="100000.00"></div>
        <div class="full"><button type="submit">{esc(self.t('btn.create_budget'))}</button></div></div></form>
        <p class="muted small">{esc(self.t('misc.full_wbs_note'))}</p></div>"""
        self.send_html(self.page(self.t('nav.budgets'), body))

    def budget_detail(self, budget_id):
        with db() as conn:
            m = budget_metrics(conn, budget_id)
            if not m:
                return self.send_html(self.page(self.t("title.not_found"), f"<h1>{esc(self.t('misc.budget_not_found'))}</h1>"), 404)
            r = m["row"]
            mm = monthly_metrics(conn, budget_id)
            budgets = conn.execute("SELECT id,code,name,currency FROM budget_lines WHERE id<>? ORDER BY code", (budget_id,)).fetchall()
            pos = conn.execute(
                """SELECT po.*, COALESCE(SUM(e.amount_cents),0) spent FROM purchase_orders po
                   LEFT JOIN expenses e ON e.po_id=po.id WHERE po.budget_id=? GROUP BY po.id ORDER BY po.id DESC""", (budget_id,)
            ).fetchall()
            expenses = conn.execute(
                """SELECT e.*, po.number po_number FROM expenses e LEFT JOIN purchase_orders po ON po.id=e.po_id
                   WHERE e.budget_id=? ORDER BY e.expense_date DESC,e.id DESC""", (budget_id,)
            ).fetchall()
            ops = conn.execute(
                """SELECT o.*, s.code source_code, t.code target_code FROM budget_operations o
                   LEFT JOIN budget_lines s ON s.id=o.source_budget_id LEFT JOIN budget_lines t ON t.id=o.target_budget_id
                   WHERE o.source_budget_id=? OR o.target_budget_id=? ORDER BY o.id DESC LIMIT 20""", (budget_id,budget_id)
            ).fetchall()
            w = wbs_row(conn, r["wbs_id"]) if r["wbs_id"] else None
            ce_code = conn.execute("SELECT code FROM cost_elements WHERE id=?", (r["cost_element_id"],)).fetchone()["code"] if r["cost_element_id"] else ""
        if w:
            wbs_meta = self.t("misc.wbs_meta",
                              full=f'<a href="/wbs/{w["id"]}"><strong>{esc(full_wbs(w["le_code"], w["code"]))}</strong></a>',
                              le=f'<a href="/legal-entities/{w["le_id"]}">{esc(w["le_code"])}</a>',
                              cc=f'<a href="/cost-centers/{w["cc_id"]}">{esc(w["cc_code"])}</a>')
            wbs_meta = f'<p class="muted">{wbs_meta} · {esc(self.t("col.ce"))}: {esc(ce_code or "—")}</p>'
        else:
            wbs_meta = f'<p class="warn small">{esc(self.t("misc.budget_wbs_missing"))}</p>'
        target_options = "".join(f'<option value="{b["id"]}">{esc(b["code"])} — {esc(b["name"])}</option>' for b in budgets)
        po_rows = "".join(
            f"<tr><td>{esc(p['number'])}</td><td>{esc(p['vendor'])}</td><td>{esc(p['description'])}</td><td><span class='badge {p['status']}'>{p['status']}</span></td><td>{self.money_disp(p['amount_cents'],r['currency'])}</td><td>{self.money_disp(p['spent'],r['currency'])}</td></tr>" for p in pos
        ) or f"<tr><td colspan='6' class='muted'>{esc(self.t('empty.pos'))}</td></tr>"
        exp_rows = "".join(
            f"<tr><td>{esc(e['expense_date'])}</td><td>{esc(e['invoice_no'])}</td><td>{esc(e['description'])}</td><td>{esc(e['po_number'] or self.t('misc.no_po'))}</td><td>{self.money_disp(e['amount_cents'],r['currency'])}</td></tr>" for e in expenses
        ) or f"<tr><td colspan='5' class='muted'>{esc(self.t('empty.expenses'))}</td></tr>"
        op_rows = "".join(
            f"<tr><td>{esc(o['created_at'][:10])}</td><td>{esc(o['operation_type'])}</td><td>{esc(o['source_code'])}</td><td>{esc(o['target_code'])}</td><td>{self.money_disp(o['amount_cents'],r['currency'])}</td><td>{esc(o['note'])}</td></tr>" for o in ops
        ) or f"<tr><td colspan='6' class='muted'>{esc(self.t('empty.operations'))}</td></tr>"
        month_rows = ""
        for mo in mm["months"]:
            if mm["has_plan"]:
                badge = "OVER" if mo["over"] else "OK"
                status = f"<span class='badge {badge}'>{esc(self.t('status.month_over' if mo['over'] else 'status.month_ok'))}</span>"
                rem_cls = "bad" if mo["remaining"] < 0 else "good"
            else:
                status, rem_cls = "—", "muted"
            row_cls = " class='over'" if mo["over"] else ""
            month_name = esc(self.t("month.%d" % mo["month"]))
            month_rows += (f"<tr{row_cls}><td>{month_name}</td>"
                           f"<td>{self.money_disp(mo['allocated'], r['currency'])}</td>"
                           f"<td>{self.money_disp(mo['actuals'], r['currency'])}</td>"
                           f"<td class='{rem_cls}'>{self.money_disp(mo['remaining'], r['currency'])}</td>"
                           f"<td>{status}</td></tr>")
        month_rows += (f"<tr><td><strong>{esc(self.t('misc.total'))}</strong></td>"
                       f"<td><strong>{self.money_disp(mm['allocated_total'], r['currency'])}</strong></td>"
                       f"<td><strong>{self.money_disp(mm['actuals_in_year'], r['currency'])}</strong></td>"
                       f"<td><strong>{self.money_disp(mm['allocated_total'] - mm['actuals_in_year'], r['currency'])}</strong></td><td></td></tr>")
        monthly_notes = ""
        if not mm["has_plan"]:
            monthly_notes += f"<p class='muted small'>{esc(self.t('misc.no_monthly_plan'))}</p>"
        elif mm["allocated_total"] != m["released"]:
            monthly_notes += (f"<p class='warn small'>{esc(self.t('misc.alloc_vs_released', allocated=self.money(mm['allocated_total'], r['currency']), released=self.money(m['released'], r['currency'])))}</p>")
        if mm["actuals_out_of_year"]:
            monthly_notes += (f"<p class='muted small'>{esc(self.t('misc.out_of_year_actuals', year=r['fiscal_year'], money=self.money(mm['actuals_out_of_year'], r['currency'])))}</p>")
        alloc_inputs = "".join(
            f"<div><label>{esc(self.t('month.%d' % mo['month']))}</label>"
            f"<input name='alloc_{mo['month']}' value='{cents_to_input(mo['allocated']) if mm['has_plan'] else ''}'></div>"
            for mo in mm["months"])
        monthly_panel = f"""<div class="panel"><h2>{esc(self.t('h2.monthly'))}</h2>{monthly_notes}
        <div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.month'))}</th><th>{esc(self.t('col.allocated'))}</th><th>{esc(self.t('col.actuals'))}</th><th>{esc(self.t('col.remaining'))}</th><th>{esc(self.t('col.status'))}</th></tr></thead><tbody>{month_rows}</tbody></table></div>
        <br><form method="post" action="/budgets/{budget_id}/allocations">{self.csrf_input()}<div class="form-grid">{alloc_inputs}
        <div class="full"><button type="submit">{esc(self.t('btn.save_allocations'))}</button> <button type="submit" name="action" value="distribute" class="secondary">{esc(self.t('btn.distribute_evenly'))}</button></div></div></form>
        <p class="muted small">{esc(self.t('misc.alloc_note'))}</p></div><br>"""
        body = f"""<div class="toolbar"><h1>{esc(r['code'])}: {esc(r['name'])}</h1>{self.currency_switcher()}<a class="button secondary" href="/budgets/{budget_id}/edit">{esc(self.t('btn.edit'))}</a></div>
        <p class="muted">{esc(self.t('col.holder'))}: {esc(r['holder_name'])}</p>{wbs_meta}
        <div class="grid cards"><div class="card"><div class="label">{esc(self.t('metric.approved'))}</div><div class="metric">{self.money_disp(m['approved'],r['currency'])}</div></div>
        <div class="card"><div class="label">{esc(self.t('metric.released'))}</div><div class="metric">{self.money_disp(m['released'],r['currency'])}</div></div>
        <div class="card"><div class="label">{esc(self.t('metric.actuals'))}</div><div class="metric">{self.money_disp(m['actuals'],r['currency'])}</div></div>
        <div class="card"><div class="label">{esc(self.t('metric.commitments'))}</div><div class="metric">{self.money_disp(m['commitments'],r['currency'])}</div></div>
        <div class="card"><div class="label">{esc(self.t('metric.available'))}</div><div class="metric {'bad' if m['available']<0 else 'good'}">{self.money_disp(m['available'],r['currency'])}</div></div></div><br>
        {monthly_panel}
        <div class="split"><div><div class="panel"><h2>{esc(self.t('h2.pos'))}</h2><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.number'))}</th><th>{esc(self.t('col.vendor'))}</th><th>{esc(self.t('col.description'))}</th><th>{esc(self.t('col.status'))}</th><th>{esc(self.t('col.amount'))}</th><th>{esc(self.t('col.actuals'))}</th></tr></thead><tbody>{po_rows}</tbody></table></div></div><br>
        <div class="panel"><h2>{esc(self.t('h2.expenses'))}</h2><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.date'))}</th><th>{esc(self.t('col.invoice'))}</th><th>{esc(self.t('col.description'))}</th><th>{esc(self.t('col.po'))}</th><th>{esc(self.t('col.amount'))}</th></tr></thead><tbody>{exp_rows}</tbody></table></div></div></div>
        <aside><div class="panel"><h2>{esc(self.t('h2.budget_operation'))}</h2><form method="post" action="/budgets/{budget_id}/operation">{self.csrf_input()}
        <label>{esc(self.t('label.op_type'))}</label><select name="operation_type" required>{self.op_type_options()}</select><br>
        <label>{esc(self.t('label.amount'))}</label><input name="amount" required><br><label>{esc(self.t('label.target_transfer'))}</label><select name="target_budget_id"><option value="">—</option>{target_options}</select><br>
        <label>{esc(self.t('label.basis'))}</label><textarea name="note" required></textarea><br><label>{esc(self.t('label.executor'))}</label><input name="created_by" value="Budget Holder" required><br><button type="submit">{esc(self.t('btn.run_operation'))}</button></form></div></aside></div><br>
        <div class="panel"><h2>{esc(self.t('h2.operations_log'))}</h2><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.date'))}</th><th>{esc(self.t('col.operation'))}</th><th>{esc(self.t('col.source'))}</th><th>{esc(self.t('col.target'))}</th><th>{esc(self.t('col.amount'))}</th><th>{esc(self.t('col.basis'))}</th></tr></thead><tbody>{op_rows}</tbody></table></div></div>"""
        self.send_html(self.page(r["code"], body))

    def pos_page(self):
        with db() as conn:
            pos = conn.execute(
                """SELECT po.*,b.code,b.currency,COALESCE(SUM(e.amount_cents),0) spent FROM purchase_orders po
                   JOIN budget_lines b ON b.id=po.budget_id LEFT JOIN expenses e ON e.po_id=po.id
                   GROUP BY po.id ORDER BY po.id DESC"""
            ).fetchall()
            budgets = all_budget_metrics(conn)
        rows = ""
        for p in pos:
            remaining = max(p["amount_cents"] - p["spent"], 0) if p["status"] == "APPROVED" else 0
            actions = ""
            if p["status"] == "DRAFT":
                actions = self.status_form(p["id"], "APPROVED", self.t("action.approve")) + " " + self.status_form(p["id"], "CANCELLED", self.t("action.cancel"), "danger")
            elif p["status"] == "APPROVED":
                actions = self.status_form(p["id"], "CLOSED", self.t("action.close"), "secondary") + " " + self.status_form(p["id"], "CANCELLED", self.t("action.cancel"), "danger")
            rows += f"<tr><td><a href='/pos/{p['id']}'>{esc(p['number'])}</a></td><td><a href='/budgets/{p['budget_id']}'>{esc(p['code'])}</a></td><td>{esc(p['vendor'])}</td><td>{esc(p['description'])}</td><td><span class='badge {p['status']}'>{p['status']}</span></td><td>{self.money_disp(p['amount_cents'],p['currency'])}</td><td>{self.money_disp(p['spent'],p['currency'])}</td><td>{self.money_disp(remaining,p['currency'])}</td><td>{actions}</td></tr>"
        budget_options = "".join(f'<option value="{m["row"]["id"]}">{self.t("opt.available", code=esc(m["row"]["code"]), money=self.money(m["available"],m["row"]["currency"]))}</option>' for m in budgets)
        body = f"""<div class="toolbar"><h1>{esc(self.t('h1.pos'))}</h1>{self.currency_switcher()}</div><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.number'))}</th><th>{esc(self.t('col.budget'))}</th><th>{esc(self.t('col.vendor'))}</th><th>{esc(self.t('col.content'))}</th><th>{esc(self.t('col.status'))}</th><th>{esc(self.t('col.amount'))}</th><th>{esc(self.t('col.actuals'))}</th><th>{esc(self.t('col.commitment'))}</th><th>{esc(self.t('col.actions'))}</th></tr></thead><tbody>{rows}</tbody></table></div><br>
        <div class="panel"><h2>{esc(self.t('h2.create_po'))}</h2><form method="post" action="/pos/new">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.number'))} *</label><input name="number" required placeholder="PO-2026-0002"></div><div><label>{esc(self.t('label.budget'))} *</label><select name="budget_id" required>{budget_options}</select></div>
        <div><label>{esc(self.t('label.vendor'))} *</label><input name="vendor" required></div><div><label>{esc(self.t('label.amount_limit'))} *</label><input name="amount" required></div>
        <div><label>{esc(self.t('label.status'))}</label><select name="status"><option value="DRAFT">{esc(self.t('opt.po_draft'))}</option><option value="APPROVED">{esc(self.t('opt.po_approved'))}</option></select></div>
        <div class="full"><label>{esc(self.t('label.content'))} *</label><textarea name="description" required placeholder="{esc(self.t('ph.po_content'))}"></textarea></div>
        <div class="full"><button type="submit">{esc(self.t('btn.create_po'))}</button></div></div></form></div>"""
        self.send_html(self.page(self.t('nav.pos'), body))

    def status_form(self, po_id, status, label, cls=""):
        return f'<form class="inline" method="post" action="/pos/{po_id}/status">{self.csrf_input()}<input type="hidden" name="status" value="{status}"><button class="{cls}" type="submit">{esc(label)}</button></form>'

    def expenses_page(self):
        month = parse_qs(urlparse(self.path).query).get("month", [""])[0]
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            month = ""
        month_where = "WHERE substr(e.expense_date,1,7)=?" if month else ""
        with db() as conn:
            expenses = conn.execute(
                f"""SELECT e.*,b.code,b.currency,po.number po_number FROM expenses e JOIN budget_lines b ON b.id=e.budget_id
                   LEFT JOIN purchase_orders po ON po.id=e.po_id {month_where} ORDER BY e.expense_date DESC,e.id DESC""",
                (month,) if month else (),
            ).fetchall()
            budgets = all_budget_metrics(conn)
            pos = conn.execute(
                """SELECT po.id,po.number,po.budget_id,po.amount_cents,b.currency,COALESCE(SUM(e.amount_cents),0) spent
                   FROM purchase_orders po JOIN budget_lines b ON b.id=po.budget_id LEFT JOIN expenses e ON e.po_id=po.id
                   WHERE po.status='APPROVED' GROUP BY po.id ORDER BY po.number"""
            ).fetchall()
        rows = "".join(f"<tr><td>{esc(e['expense_date'])}</td><td>{esc(e['code'])}</td><td>{esc(e['po_number'] or self.t('misc.no_po'))}</td><td>{esc(e['invoice_no'])}</td><td>{esc(e['description'])}</td><td>{self.money_disp(e['amount_cents'],e['currency'])}</td><td><a class='button secondary' href='/expenses/{e['id']}'>{esc(self.t('btn.open'))}</a></td></tr>" for e in expenses)
        budget_options = "".join(f'<option value="{m["row"]["id"]}">{self.t("opt.available", code=esc(m["row"]["code"]), money=self.money(m["available"],m["row"]["currency"]))}</option>' for m in budgets)
        po_options = "".join(f'<option value="{p["id"]}">{self.t("opt.remaining", number=esc(p["number"]), money=self.money(max(p["amount_cents"]-p["spent"],0),p["currency"]))}</option>' for p in pos)
        ccy_keep = ""
        self.ensure_display_context()
        if self.display_ccy != self.base_ccy:
            ccy_keep = f'<input type="hidden" name="ccy" value="{esc(self.display_ccy)}">'
        clear_link = f' <a class="button secondary" href="/expenses">{esc(self.t("btn.clear_filter"))}</a>' if month else ""
        month_filter = (f'<form class="inline" method="get"><input type="month" name="month" value="{esc(month)}">{ccy_keep}'
                        f'<button class="secondary" type="submit">{esc(self.t("btn.apply_filter"))}</button></form>{clear_link}')
        body = f"""<div class="toolbar"><h1>{esc(self.t('h1.expenses'))}</h1><div class="ccysw">{month_filter}</div>{self.currency_switcher()}</div><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.date'))}</th><th>{esc(self.t('col.budget'))}</th><th>{esc(self.t('col.po'))}</th><th>{esc(self.t('col.invoice'))}</th><th>{esc(self.t('col.description'))}</th><th>{esc(self.t('col.amount'))}</th><th>{esc(self.t('col.actions'))}</th></tr></thead><tbody>{rows}</tbody></table></div><br>
        <div class="panel"><h2>{esc(self.t('h2.add_expense'))}</h2><form method="post" action="/expenses/new">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.budget'))} *</label><select name="budget_id" required>{budget_options}</select></div><div><label>{esc(self.t('label.po'))}</label><select name="po_id"><option value="">{esc(self.t('misc.no_po'))}</option>{po_options}</select></div>
        <div><label>{esc(self.t('label.date'))} *</label><input type="date" name="expense_date" value="{date.today().isoformat()}" required></div><div><label>{esc(self.t('label.invoice'))}</label><input name="invoice_no"></div>
        <div><label>{esc(self.t('label.amount'))} *</label><input name="amount" required></div><div class="full"><label>{esc(self.t('label.description'))} *</label><textarea name="description" required></textarea></div>
        <div class="full"><button type="submit">{esc(self.t('btn.post_expense'))}</button></div></div></form></div>"""
        self.send_html(self.page(self.t('nav.expenses'), body))

    def operations_page(self):
        with db() as conn:
            ops = conn.execute(
                """SELECT o.*,s.code source_code,s.currency source_currency,s.wbs_id source_wbs,
                          t.code target_code,t.currency target_currency,t.wbs_id target_wbs
                   FROM budget_operations o LEFT JOIN budget_lines s ON s.id=o.source_budget_id
                   LEFT JOIN budget_lines t ON t.id=o.target_budget_id ORDER BY o.id DESC"""
            ).fetchall()
            wbs_by_id = wbs_index(conn)

        def budget_cell(code, wbs_id):
            # Every budget change is anchored to a full WBS; show it under the
            # budget code so the log reads without opening each line.
            w = wbs_by_id.get(wbs_id)
            sub = (f'<div class="small muted">{esc(full_wbs(w["le_code"], w["code"]))}</div>') if w else ""
            return f"{esc(code)}{sub}"

        rows = "".join(f"<tr><td>{esc(o['created_at'])}</td><td>{esc(o['operation_type'])}</td><td>{budget_cell(o['source_code'], o['source_wbs'])}</td><td>{budget_cell(o['target_code'], o['target_wbs'])}</td><td>{self.money_disp(o['amount_cents'],o['source_currency'] or o['target_currency'] or '')}</td><td>{esc(o['created_by'])}</td><td>{esc(o['note'])}</td><td><a class='button secondary' href='/operations/{o['id']}'>{esc(self.t('btn.open'))}</a></td></tr>" for o in ops)
        body = f"""<div class="toolbar"><h1>{esc(self.t('h1.operations'))}</h1>{self.currency_switcher()}</div><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.date'))}</th><th>{esc(self.t('col.operation'))}</th><th>{esc(self.t('col.source'))}</th><th>{esc(self.t('col.target'))}</th><th>{esc(self.t('col.amount'))}</th><th>{esc(self.t('col.executor'))}</th><th>{esc(self.t('col.basis'))}</th><th>{esc(self.t('col.actions'))}</th></tr></thead><tbody>{rows}</tbody></table></div>"""
        self.send_html(self.page(self.t('nav.operations'), body))

    def create_budget(self, data):
        code = require(data, "code", self.t("field.code"), self.lang)
        name = require(data, "name", self.t("field.name"), self.lang)
        holder_name = require(data, "holder_name", self.t("field.holder"), self.lang)
        fiscal_year = parse_int(data.get("fiscal_year"), self.t("field.fiscal_year"), self.lang)
        approved = money_to_cents(data.get("approved"), self.lang)
        released = money_to_cents(data.get("released"), self.lang)
        if released > approved:
            raise ValueError(self.t("error.released_gt_approved_input"))
        currency = data.get("currency", "EUR").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError(self.t("error.currency_format"))
        wbs_id = parse_int(data.get("wbs_id"), self.t("field.wbs_code"), self.lang)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with db(write=True) as conn:
            if not conn.execute("SELECT 1 FROM currencies WHERE code=? AND is_active=1", (currency,)).fetchone():
                raise ValueError(self.t("error.currency_not_active"))
            w = wbs_row(conn, wbs_id)
            if not w:
                raise ValueError(self.t("error.wbs_not_found"))
            conn.execute(
                """INSERT INTO budget_lines(code,name,fiscal_year,holder_name,holder_email,cost_center,wbs,cost_element,currency,initial_approved_cents,initial_released_cents,created_at,wbs_id,cost_element_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (code, name, fiscal_year, holder_name, data.get("holder_email","").strip(),
                 w["cc_code"], w["code"], "", currency, approved, released, now, wbs_id, None),
            )
        self.redirect("/budgets", self.t("flash.budget_created"))

    def save_allocations(self, budget_id, data):
        with db(write=True) as conn:
            m = budget_metrics(conn, budget_id)
            if not m:
                raise ValueError(self.t("error.budget_not_found"))
            if data.get("action") == "distribute":
                values = spread_evenly(m["released"])
            else:
                values = [money_to_cents_or_zero(data.get(f"alloc_{i}"), self.lang) for i in range(1, 13)]
            conn.execute("DELETE FROM budget_monthly_allocations WHERE budget_id=?", (budget_id,))
            if any(values):
                conn.executemany(
                    "INSERT INTO budget_monthly_allocations(budget_id,month,allocated_cents) VALUES(?,?,?)",
                    [(budget_id, i + 1, v) for i, v in enumerate(values)],
                )
                msg = self.t("flash.allocations_saved")
            else:
                msg = self.t("flash.allocations_cleared")
        self.redirect(f"/budgets/{budget_id}", msg)

    def create_operation(self, budget_id, data):
        op = data.get("operation_type", "").upper()
        amount = money_to_cents(data.get("amount"), self.lang)
        target_id = parse_int(data.get("target_budget_id"), self.t("field.target_budget"), self.lang) if data.get("target_budget_id") else None
        allowed = {"SUPPLEMENT","REDUCTION","RELEASE","RETURN","TRANSFER","CARRY_FORWARD"}
        if op not in allowed:
            raise ValueError(self.t("error.unknown_operation"))
        with db(write=True) as conn:
            source = budget_metrics(conn, budget_id)
            if not source:
                raise ValueError(self.t("error.budget_not_found"))
            target = None
            if op in {"TRANSFER", "CARRY_FORWARD"}:
                if not target_id or target_id == budget_id:
                    raise ValueError(self.t("error.choose_other_target"))
                target = budget_metrics(conn, target_id)
            sa, sr, ta, tr = compute_operation_deltas(op, amount, source, target)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            conn.execute(
                """INSERT INTO budget_operations(operation_type,source_budget_id,target_budget_id,amount_cents,approved_delta_source,released_delta_source,approved_delta_target,released_delta_target,note,created_by,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (op,budget_id,target_id,amount,sa,sr,ta,tr,data.get("note","").strip(),data.get("created_by","Budget Holder").strip(),now),
            )
        self.redirect(f"/budgets/{budget_id}", self.t("flash.operation_done"))

    def create_po(self, data):
        number = require(data, "number", self.t("field.po_number"), self.lang)
        vendor = require(data, "vendor", self.t("field.vendor"), self.lang)
        description = require(data, "description", self.t("field.content"), self.lang)
        budget_id = parse_int(data.get("budget_id"), self.t("field.budget"), self.lang)
        amount = money_to_cents(data.get("amount"), self.lang)
        status = data.get("status", "DRAFT").upper()
        if status not in {"DRAFT","APPROVED"}:
            raise ValueError(self.t("error.bad_po_status"))
        with db(write=True) as conn:
            m = budget_metrics(conn, budget_id)
            if not m:
                raise ValueError(self.t("error.budget_not_found"))
            if status == "APPROVED" and amount > m["available"]:
                raise ValueError(self.t("error.insufficient_po_approve"))
            now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            conn.execute(
                "INSERT INTO purchase_orders(number,budget_id,vendor,description,amount_cents,status,created_at) VALUES(?,?,?,?,?,?,?)",
                (number,budget_id,vendor,description,amount,status,now),
            )
        self.redirect("/pos", self.t("flash.po_created"))

    def change_po_status(self, po_id, data):
        new_status = data.get("status", "").upper()
        if new_status not in {"APPROVED","CLOSED","CANCELLED"}:
            raise ValueError(self.t("error.bad_status"))
        with db(write=True) as conn:
            po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
            if not po:
                raise ValueError(self.t("error.po_not_found"))
            if new_status == "APPROVED":
                if po["status"] != "DRAFT":
                    raise ValueError(self.t("error.approve_only_draft"))
                m = budget_metrics(conn, po["budget_id"])
                spent = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM expenses WHERE po_id=?", (po_id,)).fetchone()[0]
                remaining = max(po["amount_cents"] - spent, 0)
                if remaining > m["available"]:
                    raise ValueError(self.t("error.insufficient_available"))
            elif new_status in {"CLOSED","CANCELLED"} and po["status"] not in {"DRAFT","APPROVED"}:
                raise ValueError(self.t("error.po_already_closed"))
            conn.execute("UPDATE purchase_orders SET status=? WHERE id=?", (new_status, po_id))
        self.redirect("/pos", self.t("flash.po_status_changed"))

    def create_expense(self, data):
        budget_id = parse_int(data.get("budget_id"), self.t("field.budget"), self.lang)
        po_id = parse_int(data.get("po_id"), self.t("field.po"), self.lang) if data.get("po_id") else None
        expense_date = parse_date(data.get("expense_date"), self.lang)
        description = require(data, "description", self.t("field.description"), self.lang)
        amount = money_to_cents(data.get("amount"), self.lang)
        with db(write=True) as conn:
            m = budget_metrics(conn, budget_id)
            if not m:
                raise ValueError(self.t("error.budget_not_found"))
            if po_id:
                po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
                if not po or po["budget_id"] != budget_id:
                    raise ValueError(self.t("error.po_not_in_budget"))
                if po["status"] != "APPROVED":
                    raise ValueError(self.t("error.expense_needs_approved_po"))
                spent = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM expenses WHERE po_id=?", (po_id,)).fetchone()[0]
                if spent + amount > po["amount_cents"]:
                    raise ValueError(self.t("error.expense_exceeds_po"))
            else:
                if amount > m["available"]:
                    raise ValueError(self.t("error.insufficient_no_po"))
            now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
            conn.execute(
                "INSERT INTO expenses(budget_id,po_id,expense_date,invoice_no,description,amount_cents,created_at) VALUES(?,?,?,?,?,?,?)",
                (budget_id,po_id,expense_date,data.get("invoice_no","").strip(),description,amount,now),
            )
            over = month_overspent(conn, budget_id, expense_date)
        if over:
            self.redirect("/expenses", self.t("flash.expense_posted_over_month"), kind="warn")
        else:
            self.redirect("/expenses", self.t("flash.expense_posted"))

    # ------------------------------------------------------------------ #
    # Budgets: update / delete                                           #
    # ------------------------------------------------------------------ #
    def budget_edit_page(self, budget_id):
        with db() as conn:
            m = budget_metrics(conn, budget_id)
            if not m:
                return self.send_html(self.page(self.t("title.not_found"), f"<h1>{esc(self.t('misc.budget_not_found'))}</h1>"), 404)
            r = m["row"]
            linked = conn.execute(
                """SELECT (SELECT COUNT(*) FROM purchase_orders WHERE budget_id=?)
                        + (SELECT COUNT(*) FROM expenses WHERE budget_id=?)
                        + (SELECT COUNT(*) FROM budget_operations WHERE source_budget_id=? OR target_budget_id=?)""",
                (budget_id, budget_id, budget_id, budget_id),
            ).fetchone()[0]
            wbs_opts = self.wbs_options(conn, r["wbs_id"])
            # The cost element list is narrowed to the ones this WBS carries.
            # There is no client-side JS (CSP forbids it), so the filtering
            # happens here, at render time, against the WBS currently saved.
            ce_rows = conn.execute(
                """SELECT c.id,c.code,c.name FROM wbs_cost_elements l JOIN cost_elements c ON c.id=l.cost_element_id
                   WHERE l.wbs_id=? ORDER BY c.code""", (r["wbs_id"],)).fetchall() if r["wbs_id"] else []
        ce_opts = '<option value="">—</option>' + "".join(
            f'<option value="{c["id"]}"{" selected" if c["id"] == r["cost_element_id"] else ""}>'
            f'{esc(c["code"])} — {esc(c["name"])}</option>' for c in ce_rows)
        if linked:
            delete_block = f'<p class="muted small">{esc(self.t("misc.budget_delete_blocked", linked=linked))}</p>'
        else:
            delete_block = (f'<form method="post" action="/budgets/{budget_id}/delete">{self.csrf_input()}'
                            f'<button class="danger" type="submit">{esc(self.t("btn.delete_budget"))}</button></form>')
        body = f"""<div class="toolbar"><h1>{self.t('misc.h1_budget_edit', code=esc(r['code']))}</h1><a class="button secondary" href="/budgets/{budget_id}">{esc(self.t('btn.back_to_budget'))}</a></div>
        <div class="panel"><form method="post" action="/budgets/{budget_id}/edit">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.code'))} *</label><input name="code" required value="{esc(r['code'])}"></div><div><label>{esc(self.t('label.name'))} *</label><input name="name" required value="{esc(r['name'])}"></div>
        <div><label>{esc(self.t('label.fiscal_year'))} *</label><input type="number" name="fiscal_year" required value="{r['fiscal_year']}"></div><div><label>{esc(self.t('label.currency'))} *</label><select name="currency" required>{self.currency_options(r['currency'], include=r['currency'])}</select></div>
        <div><label>{esc(self.t('label.holder'))} *</label><input name="holder_name" required value="{esc(r['holder_name'])}"></div><div><label>{esc(self.t('label.email'))}</label><input type="email" name="holder_email" value="{esc(r['holder_email'])}"></div>
        <div><label>{esc(self.t('label.wbs_sel'))} *</label><select name="wbs_id" required>{wbs_opts}</select></div><div><label>{esc(self.t('label.cost_element'))}</label><select name="cost_element_id">{ce_opts}</select></div>
        <div><label>{esc(self.t('label.approved'))} *</label><input name="approved" required value="{cents_to_input(r['initial_approved_cents'])}"></div><div><label>{esc(self.t('label.released'))} *</label><input name="released" required value="{cents_to_input(r['initial_released_cents'])}"></div>
        <div class="full"><button type="submit">{esc(self.t('btn.save_changes'))}</button></div></div></form>
        <p class="muted small">{esc(self.t('misc.edit_budget_note'))}</p></div>
        <br><div class="panel"><h2>{esc(self.t('h2.deletion'))}</h2>{delete_block}</div>"""
        self.send_html(self.page(self.t("title.budget_edit"), body))

    def update_budget(self, budget_id, data):
        code = require(data, "code", self.t("field.code"), self.lang)
        name = require(data, "name", self.t("field.name"), self.lang)
        holder_name = require(data, "holder_name", self.t("field.holder"), self.lang)
        fiscal_year = parse_int(data.get("fiscal_year"), self.t("field.fiscal_year"), self.lang)
        approved = money_to_cents(data.get("approved"), self.lang)
        released = money_to_cents(data.get("released"), self.lang)
        if released > approved:
            raise ValueError(self.t("error.released_gt_approved_input"))
        currency = data.get("currency", "EUR").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError(self.t("error.currency_format"))
        wbs_id = parse_int(data.get("wbs_id"), self.t("field.wbs_code"), self.lang)
        ce_raw = (data.get("cost_element_id") or "").strip()
        ce_id = parse_int(ce_raw, self.t("field.cost_element"), self.lang) if ce_raw else None
        with db(write=True) as conn:
            existing = conn.execute("SELECT currency FROM budget_lines WHERE id=?", (budget_id,)).fetchone()
            if not existing:
                raise ValueError(self.t("error.budget_not_found"))
            if currency != existing["currency"] and not conn.execute(
                    "SELECT 1 FROM currencies WHERE code=? AND is_active=1", (currency,)).fetchone():
                raise ValueError(self.t("error.currency_not_active"))
            w = wbs_row(conn, wbs_id)
            if not w:
                raise ValueError(self.t("error.wbs_not_found"))
            # A cost element is only valid on this line if the chosen WBS
            # carries it; picking a WBS that does not clears the field rather
            # than storing a pairing the handbook does not know about.
            if ce_id is not None and ce_id not in wbs_ce_ids(conn, wbs_id):
                raise ValueError(self.t("error.ce_not_in_wbs"))
            ce_code = conn.execute("SELECT code FROM cost_elements WHERE id=?", (ce_id,)).fetchone()["code"] if ce_id else ""
            conn.execute(
                """UPDATE budget_lines SET code=?,name=?,fiscal_year=?,holder_name=?,holder_email=?,
                   cost_center=?,wbs=?,cost_element=?,currency=?,initial_approved_cents=?,initial_released_cents=?,
                   wbs_id=?,cost_element_id=? WHERE id=?""",
                (code, name, fiscal_year, holder_name, data.get("holder_email", "").strip(),
                 w["cc_code"], w["code"], ce_code,
                 currency, approved, released, wbs_id, ce_id, budget_id),
            )
            assert_budget_ok(conn, budget_id)
        self.redirect(f"/budgets/{budget_id}", self.t("flash.budget_updated"))

    def delete_budget(self, budget_id, data):
        with db(write=True) as conn:
            if not conn.execute("SELECT 1 FROM budget_lines WHERE id=?", (budget_id,)).fetchone():
                raise ValueError(self.t("error.budget_not_found"))
            linked = conn.execute(
                """SELECT (SELECT COUNT(*) FROM purchase_orders WHERE budget_id=?)
                        + (SELECT COUNT(*) FROM expenses WHERE budget_id=?)
                        + (SELECT COUNT(*) FROM budget_operations WHERE source_budget_id=? OR target_budget_id=?)""",
                (budget_id, budget_id, budget_id, budget_id),
            ).fetchone()[0]
            if linked:
                raise ValueError(self.t("error.cannot_delete_budget_linked"))
            # The monthly plan is attached data, not a transaction document: it
            # never blocks deletion and goes away with the line (the FK would
            # otherwise reject the delete).
            conn.execute("DELETE FROM budget_monthly_allocations WHERE budget_id=?", (budget_id,))
            conn.execute("DELETE FROM budget_lines WHERE id=?", (budget_id,))
        self.redirect("/budgets", self.t("flash.budget_deleted"))

    # ------------------------------------------------------------------ #
    # Purchase orders: read / update / delete                           #
    # ------------------------------------------------------------------ #
    def po_detail(self, po_id):
        with db() as conn:
            po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
            if not po:
                return self.send_html(self.page(self.t("title.not_found"), f"<h1>{esc(self.t('misc.po_not_found'))}</h1>"), 404)
            budget = conn.execute("SELECT * FROM budget_lines WHERE id=?", (po["budget_id"],)).fetchone()
            spent = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM expenses WHERE po_id=?", (po_id,)).fetchone()[0]
            exp_count = conn.execute("SELECT COUNT(*) FROM expenses WHERE po_id=?", (po_id,)).fetchone()[0]
            budgets = all_budget_metrics(conn)
        cur = budget["currency"]
        commitment = max(po["amount_cents"] - spent, 0) if po["status"] == "APPROVED" else 0
        if po["status"] == "DRAFT":
            actions = self.status_form(po["id"], "APPROVED", self.t("action.approve")) + " " + self.status_form(po["id"], "CANCELLED", self.t("action.cancel"), "danger")
        elif po["status"] == "APPROVED":
            actions = self.status_form(po["id"], "CLOSED", self.t("action.close"), "secondary") + " " + self.status_form(po["id"], "CANCELLED", self.t("action.cancel"), "danger")
        else:
            actions = '<span class="muted">—</span>'
        if po["status"] in {"DRAFT", "APPROVED"}:
            budget_options = "".join(
                f'<option value="{m["row"]["id"]}"{" selected" if m["row"]["id"] == po["budget_id"] else ""}>{self.t("opt.available", code=esc(m["row"]["code"]), money=self.money(m["available"], m["row"]["currency"]))}</option>'
                for m in budgets)
            edit_block = f"""<div class="panel"><h2>{esc(self.t('h2.edit_po'))}</h2><form method="post" action="/pos/{po_id}/edit">{self.csrf_input()}<div class="form-grid">
            <div><label>{esc(self.t('label.number'))} *</label><input name="number" required value="{esc(po['number'])}"></div><div><label>{esc(self.t('label.budget'))} *</label><select name="budget_id" required>{budget_options}</select></div>
            <div><label>{esc(self.t('label.vendor'))} *</label><input name="vendor" required value="{esc(po['vendor'])}"></div><div><label>{esc(self.t('label.amount_limit'))} *</label><input name="amount" required value="{cents_to_input(po['amount_cents'])}"></div>
            <div class="full"><label>{esc(self.t('label.content'))} *</label><textarea name="description" required>{esc(po['description'])}</textarea></div>
            <div class="full"><button type="submit">{esc(self.t('btn.save'))}</button></div></div></form></div><br>"""
        else:
            edit_block = f'<div class="panel muted">{esc(self.t("misc.po_not_editable", status=po["status"]))}</div><br>'
        if exp_count:
            delete_block = f'<p class="muted small">{esc(self.t("misc.po_delete_blocked", count=exp_count))}</p>'
        else:
            delete_block = (f'<form method="post" action="/pos/{po_id}/delete">{self.csrf_input()}'
                            f'<button class="danger" type="submit">{esc(self.t("btn.delete_po"))}</button></form>')
        body = f"""<div class="toolbar"><h1>{self.t('misc.h1_po', number=esc(po['number']))}</h1>{self.currency_switcher()}<a class="button secondary" href="/pos">{esc(self.t('btn.back_to_pos'))}</a></div>
        <div class="panel"><p><span class="badge {po['status']}">{po['status']}</span> · {self.t('misc.po_meta', budget=f'<a href="/budgets/{po["budget_id"]}">{esc(budget["code"])}</a>', vendor=esc(po['vendor']))}</p>
        <p>{esc(po['description'])}</p><div class="grid cards">
        <div class="card"><div class="label">{esc(self.t('label.amount_limit'))}</div><div class="metric">{self.money_disp(po['amount_cents'], cur)}</div></div>
        <div class="card"><div class="label">{esc(self.t('metric.actuals'))}</div><div class="metric">{self.money_disp(spent, cur)}</div></div>
        <div class="card"><div class="label">{esc(self.t('metric.commitment'))}</div><div class="metric">{self.money_disp(commitment, cur)}</div></div></div>
        <br><div class="toolbar">{actions}</div></div><br>
        {edit_block}<div class="panel"><h2>{esc(self.t('h2.deletion'))}</h2>{delete_block}</div>"""
        self.send_html(self.page(po["number"], body))

    def update_po(self, po_id, data):
        number = require(data, "number", self.t("field.po_number"), self.lang)
        vendor = require(data, "vendor", self.t("field.vendor"), self.lang)
        description = require(data, "description", self.t("field.content"), self.lang)
        budget_id = parse_int(data.get("budget_id"), self.t("field.budget"), self.lang)
        amount = money_to_cents(data.get("amount"), self.lang)
        with db(write=True) as conn:
            po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
            if not po:
                raise ValueError(self.t("error.po_not_found"))
            if po["status"] not in {"DRAFT", "APPROVED"}:
                raise ValueError(self.t("error.edit_only_draft_approved"))
            if not budget_metrics(conn, budget_id):
                raise ValueError(self.t("error.budget_not_found"))
            spent = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM expenses WHERE po_id=?", (po_id,)).fetchone()[0]
            if amount < spent:
                raise ValueError(self.t("error.po_amount_lt_spent"))
            if budget_id != po["budget_id"] and spent > 0:
                raise ValueError(self.t("error.cannot_change_budget_with_expenses"))
            conn.execute(
                "UPDATE purchase_orders SET number=?,budget_id=?,vendor=?,description=?,amount_cents=? WHERE id=?",
                (number, budget_id, vendor, description, amount, po_id),
            )
            assert_budget_ok(conn, po["budget_id"])
            assert_budget_ok(conn, budget_id)
        self.redirect(f"/pos/{po_id}", self.t("flash.po_updated"))

    def delete_po(self, po_id, data):
        with db(write=True) as conn:
            po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
            if not po:
                raise ValueError(self.t("error.po_not_found"))
            n = conn.execute("SELECT COUNT(*) FROM expenses WHERE po_id=?", (po_id,)).fetchone()[0]
            if n:
                raise ValueError(self.t("error.cannot_delete_po_with_expenses"))
            conn.execute("DELETE FROM purchase_orders WHERE id=?", (po_id,))
        self.redirect("/pos", self.t("flash.po_deleted"))

    # ------------------------------------------------------------------ #
    # Expenses: read / update / delete                                  #
    # ------------------------------------------------------------------ #
    def expense_detail(self, expense_id):
        with db() as conn:
            e = conn.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
            if not e:
                return self.send_html(self.page(self.t("title.not_found"), f"<h1>{esc(self.t('misc.expense_not_found'))}</h1>"), 404)
            budgets = all_budget_metrics(conn)
            pos = conn.execute(
                """SELECT po.id,po.number,po.budget_id,po.amount_cents,po.status,b.currency,
                          COALESCE(SUM(x.amount_cents),0) spent
                   FROM purchase_orders po JOIN budget_lines b ON b.id=po.budget_id
                   LEFT JOIN expenses x ON x.po_id=po.id
                   WHERE po.status='APPROVED' OR po.id=? GROUP BY po.id ORDER BY po.number""",
                (e["po_id"] or -1,),
            ).fetchall()
        budget_options = "".join(
            f'<option value="{m["row"]["id"]}"{" selected" if m["row"]["id"] == e["budget_id"] else ""}>{self.t("opt.available", code=esc(m["row"]["code"]), money=self.money(m["available"], m["row"]["currency"]))}</option>'
            for m in budgets)
        po_options = f'<option value="">{esc(self.t("misc.no_po"))}</option>' + "".join(
            f'<option value="{p["id"]}"{" selected" if p["id"] == e["po_id"] else ""}>{self.t("opt.remaining", number=esc(p["number"]), money=self.money(max(p["amount_cents"] - p["spent"], 0), p["currency"]))}{"" if p["status"] == "APPROVED" else " (" + p["status"] + ")"}</option>'
            for p in pos)
        native_ccy = next((mm["row"]["currency"] for mm in budgets if mm["row"]["id"] == e["budget_id"]), self.base_ccy)
        body = f"""<div class="toolbar"><h1>{self.t('misc.h1_expense', id=e['id'])}</h1>{self.currency_switcher()}<a class="button secondary" href="/expenses">{esc(self.t('btn.back_to_expenses'))}</a></div>
        <p class="muted">{esc(self.t('label.amount'))}: {self.money_disp(e['amount_cents'], native_ccy)}</p>
        <div class="panel"><h2>{esc(self.t('h2.edit_expense'))}</h2><form method="post" action="/expenses/{expense_id}/edit">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.budget'))} *</label><select name="budget_id" required>{budget_options}</select></div><div><label>{esc(self.t('label.po'))}</label><select name="po_id">{po_options}</select></div>
        <div><label>{esc(self.t('label.date'))} *</label><input type="date" name="expense_date" value="{esc(e['expense_date'])}" required></div><div><label>{esc(self.t('label.invoice'))}</label><input name="invoice_no" value="{esc(e['invoice_no'])}"></div>
        <div><label>{esc(self.t('label.amount'))} *</label><input name="amount" required value="{cents_to_input(e['amount_cents'])}"></div><div class="full"><label>{esc(self.t('label.description'))} *</label><textarea name="description" required>{esc(e['description'])}</textarea></div>
        <div class="full"><button type="submit">{esc(self.t('btn.save'))}</button></div></div></form></div><br>
        <div class="panel"><h2>{esc(self.t('h2.deletion'))}</h2><form method="post" action="/expenses/{expense_id}/delete">{self.csrf_input()}<button class="danger" type="submit">{esc(self.t('btn.delete_expense'))}</button></form></div>"""
        self.send_html(self.page(self.t('misc.h1_expense', id=e['id']), body))

    def update_expense(self, expense_id, data):
        budget_id = parse_int(data.get("budget_id"), self.t("field.budget"), self.lang)
        po_id = parse_int(data.get("po_id"), self.t("field.po"), self.lang) if data.get("po_id") else None
        expense_date = parse_date(data.get("expense_date"), self.lang)
        description = require(data, "description", self.t("field.description"), self.lang)
        amount = money_to_cents(data.get("amount"), self.lang)
        with db(write=True) as conn:
            e = conn.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
            if not e:
                raise ValueError(self.t("error.expense_not_found"))
            if not budget_metrics(conn, budget_id):
                raise ValueError(self.t("error.budget_not_found"))
            if po_id:
                po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
                if not po or po["budget_id"] != budget_id:
                    raise ValueError(self.t("error.po_not_in_budget"))
                if po["status"] != "APPROVED":
                    raise ValueError(self.t("error.expense_needs_approved_po"))
            conn.execute(
                "UPDATE expenses SET budget_id=?,po_id=?,expense_date=?,invoice_no=?,description=?,amount_cents=? WHERE id=?",
                (budget_id, po_id, expense_date, data.get("invoice_no", "").strip(), description, amount, expense_id),
            )
            if po_id:
                spent = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM expenses WHERE po_id=?", (po_id,)).fetchone()[0]
                po_amount = conn.execute("SELECT amount_cents FROM purchase_orders WHERE id=?", (po_id,)).fetchone()[0]
                if spent > po_amount:
                    raise ValueError(self.t("error.expense_exceeds_po"))
            assert_budget_ok(conn, e["budget_id"])
            assert_budget_ok(conn, budget_id)
            over = month_overspent(conn, budget_id, expense_date)
        if over:
            self.redirect(f"/expenses/{expense_id}", self.t("flash.expense_updated_over_month"), kind="warn")
        else:
            self.redirect(f"/expenses/{expense_id}", self.t("flash.expense_updated"))

    def delete_expense(self, expense_id, data):
        with db(write=True) as conn:
            e = conn.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
            if not e:
                raise ValueError(self.t("error.expense_not_found"))
            conn.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
            assert_budget_ok(conn, e["budget_id"])
        self.redirect("/expenses", self.t("flash.expense_deleted"))

    # ------------------------------------------------------------------ #
    # Budget operations: read / update / delete                         #
    # ------------------------------------------------------------------ #
    def operation_detail(self, op_id):
        with db() as conn:
            o = conn.execute(
                """SELECT o.*, s.code source_code, t.code target_code FROM budget_operations o
                   LEFT JOIN budget_lines s ON s.id=o.source_budget_id
                   LEFT JOIN budget_lines t ON t.id=o.target_budget_id WHERE o.id=?""", (op_id,)
            ).fetchone()
            if not o:
                return self.send_html(self.page(self.t("title.not_found"), f"<h1>{esc(self.t('misc.operation_not_found'))}</h1>"), 404)
            source = budget_metrics(conn, o["source_budget_id"])
            targets = conn.execute("SELECT id,code,name FROM budget_lines WHERE id<>? ORDER BY code", (o["source_budget_id"],)).fetchall()
        cur = source["row"]["currency"] if source else ""
        target_options = '<option value="">—</option>' + "".join(
            f'<option value="{t["id"]}"{" selected" if t["id"] == o["target_budget_id"] else ""}>{esc(t["code"])} — {esc(t["name"])}</option>' for t in targets)
        target_line = self.t("misc.op_target", target=esc(o['target_code'])) if o["target_code"] else ""
        body = f"""<div class="toolbar"><h1>{self.t('misc.h1_operation', id=o['id'])}</h1>{self.currency_switcher()}<a class="button secondary" href="/operations">{esc(self.t('btn.back_to_operations'))}</a></div>
        <div class="panel"><p>{esc(o['operation_type'])} · {self.t('misc.op_source', source=f'<a href="/budgets/{o["source_budget_id"]}">{esc(o["source_code"])}</a>')}{target_line} · {self.money_disp(o['amount_cents'], cur)}</p>
        <p class="muted small">{self.t('misc.op_meta', created_at=esc(o['created_at']), created_by=esc(o['created_by']))}</p></div><br>
        <div class="panel"><h2>{esc(self.t('h2.edit_operation'))}</h2><form method="post" action="/operations/{op_id}/edit">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.op_type'))} *</label><select name="operation_type" required>{self.op_type_options(o["operation_type"])}</select></div><div><label>{esc(self.t('label.amount'))} *</label><input name="amount" required value="{cents_to_input(o['amount_cents'])}"></div>
        <div><label>{esc(self.t('label.target_transfer'))}</label><select name="target_budget_id">{target_options}</select></div><div><label>{esc(self.t('label.executor'))} *</label><input name="created_by" required value="{esc(o['created_by'])}"></div>
        <div class="full"><label>{esc(self.t('label.basis'))} *</label><textarea name="note" required>{esc(o['note'])}</textarea></div>
        <div class="full"><button type="submit">{esc(self.t('btn.save'))}</button></div></div></form>
        <p class="muted small">{esc(self.t('misc.edit_op_note'))}</p></div><br>
        <div class="panel"><h2>{esc(self.t('h2.deletion'))}</h2><form method="post" action="/operations/{op_id}/delete">{self.csrf_input()}<button class="danger" type="submit">{esc(self.t('btn.delete_operation'))}</button></form></div>"""
        self.send_html(self.page(self.t('misc.h1_operation', id=o['id']), body))

    def update_operation(self, op_id, data):
        op = data.get("operation_type", "").upper()
        amount = money_to_cents(data.get("amount"), self.lang)
        target_id = parse_int(data.get("target_budget_id"), self.t("field.target_budget"), self.lang) if data.get("target_budget_id") else None
        allowed = {"SUPPLEMENT", "REDUCTION", "RELEASE", "RETURN", "TRANSFER", "CARRY_FORWARD"}
        if op not in allowed:
            raise ValueError(self.t("error.unknown_operation"))
        with db(write=True) as conn:
            row = conn.execute("SELECT * FROM budget_operations WHERE id=?", (op_id,)).fetchone()
            if not row:
                raise ValueError(self.t("error.operation_not_found"))
            source_id = row["source_budget_id"]
            old_target = row["target_budget_id"]
            # Neutralise the old deltas first so the recomputed source/target
            # metrics exclude this operation and the business-rule check runs
            # against the state as if it were being posted fresh.
            conn.execute(
                """UPDATE budget_operations SET approved_delta_source=0,released_delta_source=0,
                   approved_delta_target=0,released_delta_target=0 WHERE id=?""", (op_id,))
            source = budget_metrics(conn, source_id)
            if not source:
                raise ValueError(self.t("error.source_not_found"))
            target = None
            if op in {"TRANSFER", "CARRY_FORWARD"}:
                if not target_id or target_id == source_id:
                    raise ValueError(self.t("error.choose_other_target"))
                target = budget_metrics(conn, target_id)
                if not target:
                    raise ValueError(self.t("error.target_not_found"))
            else:
                target_id = None
            sa, sr, ta, tr = compute_operation_deltas(op, amount, source, target)
            conn.execute(
                """UPDATE budget_operations SET operation_type=?,target_budget_id=?,amount_cents=?,
                   approved_delta_source=?,released_delta_source=?,approved_delta_target=?,released_delta_target=?,
                   note=?,created_by=? WHERE id=?""",
                (op, target_id, amount, sa, sr, ta, tr, data.get("note", "").strip(),
                 data.get("created_by", "Budget Holder").strip(), op_id),
            )
            assert_budget_ok(conn, source_id)
            assert_budget_ok(conn, old_target)
            assert_budget_ok(conn, target_id)
        self.redirect(f"/operations/{op_id}", self.t("flash.operation_updated"))

    def delete_operation(self, op_id, data):
        with db(write=True) as conn:
            row = conn.execute("SELECT * FROM budget_operations WHERE id=?", (op_id,)).fetchone()
            if not row:
                raise ValueError(self.t("error.operation_not_found"))
            conn.execute("DELETE FROM budget_operations WHERE id=?", (op_id,))
            assert_budget_ok(conn, row["source_budget_id"])
            assert_budget_ok(conn, row["target_budget_id"])
        self.redirect("/operations", self.t("flash.operation_deleted"))

    # ------------------------------------------------------------------ #
    # Reference data: legal entities, cost centres, WBS, cost elements    #
    # ------------------------------------------------------------------ #
    def not_found_page(self, key):
        return self.send_html(self.page(self.t("title.not_found"), f"<h1>{esc(self.t(key))}</h1>"), 404)

    def le_options(self, conn, selected=None):
        return "".join(
            f'<option value="{r["id"]}"{" selected" if r["id"] == selected else ""}>'
            f'{esc(r["code"])} — {esc(r["name"])}</option>'
            for r in conn.execute("SELECT id,code,name FROM legal_entities ORDER BY code"))

    def cc_options(self, conn, selected=None):
        # Labelled "entity / centre": cost-centre codes are unique on their own,
        # but the legal entity is what decides the full WBS, so it has to be
        # visible at the moment of choosing.
        return "".join(
            f'<option value="{r["id"]}"{" selected" if r["id"] == selected else ""}>'
            f'{esc(r["le_code"])} / {esc(r["code"])} — {esc(r["name"])}</option>'
            for r in conn.execute(
                """SELECT cc.id,cc.code,cc.name,le.code le_code FROM cost_centers cc
                   JOIN legal_entities le ON le.id=cc.legal_entity_id ORDER BY le.code, cc.code"""))

    def wbs_options(self, conn, selected=None, allow_empty=True):
        opts = f'<option value="">{esc(self.t("misc.no_wbs"))}</option>' if allow_empty else ""
        return opts + "".join(
            f'<option value="{r["id"]}"{" selected" if r["id"] == selected else ""}>'
            f'{esc(full_wbs(r["le_code"], r["code"]))} — {esc(r["name"])}</option>'
            for r in wbs_query(conn))

    def ce_checkboxes(self, conn, linked):
        rows = conn.execute("SELECT id,code,name FROM cost_elements ORDER BY code").fetchall()
        if not rows:
            return f'<p class="muted small">{esc(self.t("empty.cost_elements"))}</p>'
        return "".join(
            f'<div><label><input type="checkbox" name="ce_{r["id"]}" value="1"'
            f'{" checked" if r["id"] in linked else ""}> {esc(r["code"])} — {esc(r["name"])}</label></div>'
            for r in rows)

    def selected_ce_ids(self, conn, data):
        return [r["id"] for r in conn.execute("SELECT id FROM cost_elements")
                if data.get(f"ce_{r['id']}") == "1"]

    def handbooks_page(self):
        with db() as conn:
            n_le = conn.execute("SELECT COUNT(*) FROM legal_entities").fetchone()[0]
            n_cc = conn.execute("SELECT COUNT(*) FROM cost_centers").fetchone()[0]
            n_wbs = conn.execute("SELECT COUNT(*) FROM wbs").fetchone()[0]
            n_ce = conn.execute("SELECT COUNT(*) FROM cost_elements").fetchone()[0]
        cards = [("/legal-entities", "hb.le", "hb.le_desc", n_le),
                 ("/cost-centers", "hb.cc", "hb.cc_desc", n_cc),
                 ("/wbs", "hb.wbs", "hb.wbs_desc", n_wbs),
                 ("/cost-elements", "hb.ce", "hb.ce_desc", n_ce),
                 ("/full-wbs", "hb.full", "hb.full_desc", n_wbs)]
        items = "".join(
            f'<div class="card"><div class="label">{esc(self.t(title))}</div>'
            f'<div class="metric">{count}</div>'
            f'<p class="small muted">{esc(self.t(desc))}</p>'
            f'<a class="button secondary" href="{href}">{esc(self.t("btn.open"))}</a></div>'
            for href, title, desc, count in cards)
        body = (f'<div class="toolbar"><h1>{esc(self.t("h1.handbooks"))}</h1></div>'
                f'<p class="muted">{esc(self.t("misc.handbooks_intro"))}</p>'
                f'<div class="grid cards">{items}</div>')
        self.send_html(self.page(self.t("nav.handbooks"), body))

    # -- Legal entities ------------------------------------------------- #
    def le_page(self):
        with db() as conn:
            rows = conn.execute(
                """SELECT le.*,
                          (SELECT COUNT(*) FROM cost_centers cc WHERE cc.legal_entity_id=le.id) cc_count,
                          (SELECT COUNT(*) FROM wbs w JOIN cost_centers cc ON cc.id=w.cost_center_id
                            WHERE cc.legal_entity_id=le.id) wbs_count
                   FROM legal_entities le ORDER BY le.code"""
            ).fetchall()
        trs = "".join(
            f'<tr><td><a href="/legal-entities/{r["id"]}"><strong>{esc(r["code"])}</strong></a></td>'
            f'<td>{esc(r["name"])}</td><td>{r["cc_count"]}</td><td>{r["wbs_count"]}</td>'
            f'<td><a class="button secondary" href="/legal-entities/{r["id"]}/edit">{esc(self.t("btn.edit"))}</a></td></tr>'
            for r in rows) or f'<tr><td colspan="5" class="muted">{esc(self.t("empty.legal_entities"))}</td></tr>'
        body = f"""<div class="toolbar"><h1>{esc(self.t('h1.legal_entities'))}</h1><a class="button secondary" href="/handbooks">{esc(self.t('btn.back_to_handbooks'))}</a></div>
        <div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.code'))}</th><th>{esc(self.t('col.name'))}</th><th>{esc(self.t('col.cc_count'))}</th><th>{esc(self.t('col.wbs_count'))}</th><th>{esc(self.t('col.actions'))}</th></tr></thead><tbody>{trs}</tbody></table></div>
        <br><div class="panel"><h2>{esc(self.t('h2.create_le'))}</h2><form method="post" action="/legal-entities/new">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.code'))} *</label><input name="code" required placeholder="RU12"></div>
        <div><label>{esc(self.t('label.name'))} *</label><input name="name" required></div>
        <div class="full"><button type="submit">{esc(self.t('btn.create_le'))}</button></div></div></form>
        <p class="muted small">{esc(self.t('misc.entity_code_hint'))}</p></div>"""
        self.send_html(self.page(self.t("h1.legal_entities"), body))

    def le_detail(self, le_id):
        with db() as conn:
            le = conn.execute("SELECT * FROM legal_entities WHERE id=?", (le_id,)).fetchone()
            if not le:
                return self.not_found_page("misc.le_not_found")
            ccs = conn.execute(
                """SELECT cc.*, (SELECT COUNT(*) FROM wbs w WHERE w.cost_center_id=cc.id) wbs_count
                   FROM cost_centers cc WHERE cc.legal_entity_id=? ORDER BY cc.code""", (le_id,)).fetchall()
            wbs_rows = wbs_query(conn, "le.id=?", (le_id,))
        cc_trs = "".join(
            f'<tr><td><a href="/cost-centers/{c["id"]}">{esc(c["code"])}</a></td><td>{esc(c["name"])}</td><td>{c["wbs_count"]}</td></tr>'
            for c in ccs) or f'<tr><td colspan="3" class="muted">{esc(self.t("empty.cost_centers"))}</td></tr>'
        wbs_trs = "".join(
            f'<tr><td><a href="/wbs/{w["id"]}">{esc(full_wbs(w["le_code"], w["code"]))}</a></td>'
            f'<td>{esc(w["name"])}</td><td>{esc(w["cc_code"])}</td></tr>'
            for w in wbs_rows) or f'<tr><td colspan="3" class="muted">{esc(self.t("empty.wbs"))}</td></tr>'
        body = f"""<div class="toolbar"><h1>{esc(le['code'])}: {esc(le['name'])}</h1><a class="button secondary" href="/legal-entities/{le_id}/edit">{esc(self.t('btn.edit'))}</a><a class="button secondary" href="/legal-entities">{esc(self.t('btn.back_to_le'))}</a></div>
        <div class="panel"><h2>{esc(self.t('h2.cc_of_le'))}</h2><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.code'))}</th><th>{esc(self.t('col.name'))}</th><th>{esc(self.t('col.wbs_count'))}</th></tr></thead><tbody>{cc_trs}</tbody></table></div></div><br>
        <div class="panel"><h2>{esc(self.t('h2.wbs_of_le'))}</h2><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.full_wbs'))}</th><th>{esc(self.t('col.name'))}</th><th>{esc(self.t('col.cost_center'))}</th></tr></thead><tbody>{wbs_trs}</tbody></table></div></div>"""
        self.send_html(self.page(le["code"], body))

    def le_edit_page(self, le_id):
        with db() as conn:
            le = conn.execute("SELECT * FROM legal_entities WHERE id=?", (le_id,)).fetchone()
            if not le:
                return self.not_found_page("misc.le_not_found")
            linked = conn.execute("SELECT COUNT(*) FROM cost_centers WHERE legal_entity_id=?", (le_id,)).fetchone()[0]
        if linked:
            delete_block = f'<p class="muted small">{esc(self.t("misc.le_delete_blocked", linked=linked))}</p>'
        else:
            delete_block = (f'<form method="post" action="/legal-entities/{le_id}/delete">{self.csrf_input()}'
                            f'<button class="danger" type="submit">{esc(self.t("btn.delete_le"))}</button></form>')
        body = f"""<div class="toolbar"><h1>{self.t('misc.h1_le_edit', code=esc(le['code']))}</h1><a class="button secondary" href="/legal-entities/{le_id}">{esc(self.t('btn.back_to_le'))}</a></div>
        <div class="panel"><form method="post" action="/legal-entities/{le_id}/edit">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.code'))} *</label><input name="code" required value="{esc(le['code'])}"></div>
        <div><label>{esc(self.t('label.name'))} *</label><input name="name" required value="{esc(le['name'])}"></div>
        <div class="full"><button type="submit">{esc(self.t('btn.save_changes'))}</button></div></div></form>
        <p class="muted small">{esc(self.t('misc.entity_code_hint'))}</p></div>
        <br><div class="panel"><h2>{esc(self.t('h2.deletion'))}</h2>{delete_block}</div>"""
        self.send_html(self.page(self.t("title.le_edit"), body))

    def create_le(self, data):
        code = parse_entity_code(data, "code", self.lang)
        name = require(data, "name", self.t("field.name"), self.lang)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with db(write=True) as conn:
            conn.execute("INSERT INTO legal_entities(code,name,created_at) VALUES(?,?,?)", (code, name, now))
        self.redirect("/legal-entities", self.t("flash.le_created"))

    def update_le(self, le_id, data):
        code = parse_entity_code(data, "code", self.lang)
        name = require(data, "name", self.t("field.name"), self.lang)
        with db(write=True) as conn:
            if not conn.execute("SELECT 1 FROM legal_entities WHERE id=?", (le_id,)).fetchone():
                raise ValueError(self.t("error.le_not_found"))
            conn.execute("UPDATE legal_entities SET code=?,name=? WHERE id=?", (code, name, le_id))
        self.redirect(f"/legal-entities/{le_id}", self.t("flash.le_updated"))

    def delete_le(self, le_id, data):
        with db(write=True) as conn:
            if not conn.execute("SELECT 1 FROM legal_entities WHERE id=?", (le_id,)).fetchone():
                raise ValueError(self.t("error.le_not_found"))
            if conn.execute("SELECT COUNT(*) FROM cost_centers WHERE legal_entity_id=?", (le_id,)).fetchone()[0]:
                raise ValueError(self.t("error.cannot_delete_le_linked"))
            conn.execute("DELETE FROM legal_entities WHERE id=?", (le_id,))
        self.redirect("/legal-entities", self.t("flash.le_deleted"))

    # -- Cost centres --------------------------------------------------- #
    def cc_page(self):
        with db() as conn:
            rows = conn.execute(
                """SELECT cc.*, le.code le_code, le.id le_id,
                          (SELECT COUNT(*) FROM wbs w WHERE w.cost_center_id=cc.id) wbs_count
                   FROM cost_centers cc JOIN legal_entities le ON le.id=cc.legal_entity_id
                   ORDER BY le.code, cc.code"""
            ).fetchall()
            le_opts = self.le_options(conn)
        trs = "".join(
            f'<tr><td><a href="/cost-centers/{r["id"]}"><strong>{esc(r["code"])}</strong></a></td>'
            f'<td>{esc(r["name"])}</td><td><a href="/legal-entities/{r["le_id"]}">{esc(r["le_code"])}</a></td>'
            f'<td>{r["wbs_count"]}</td>'
            f'<td><a class="button secondary" href="/cost-centers/{r["id"]}/edit">{esc(self.t("btn.edit"))}</a></td></tr>'
            for r in rows) or f'<tr><td colspan="5" class="muted">{esc(self.t("empty.cost_centers"))}</td></tr>'
        body = f"""<div class="toolbar"><h1>{esc(self.t('h1.cost_centers'))}</h1><a class="button secondary" href="/handbooks">{esc(self.t('btn.back_to_handbooks'))}</a></div>
        <div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.code'))}</th><th>{esc(self.t('col.name'))}</th><th>{esc(self.t('col.legal_entity'))}</th><th>{esc(self.t('col.wbs_count'))}</th><th>{esc(self.t('col.actions'))}</th></tr></thead><tbody>{trs}</tbody></table></div>
        <br><div class="panel"><h2>{esc(self.t('h2.create_cc'))}</h2><form method="post" action="/cost-centers/new">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.code'))} *</label><input name="code" required placeholder="RU12-IT"></div>
        <div><label>{esc(self.t('label.name'))} *</label><input name="name" required></div>
        <div><label>{esc(self.t('label.legal_entity'))} *</label><select name="legal_entity_id" required>{le_opts}</select></div>
        <div class="full"><button type="submit">{esc(self.t('btn.create_cc'))}</button></div></div></form></div>"""
        self.send_html(self.page(self.t("h1.cost_centers"), body))

    def cc_detail(self, cc_id):
        with db() as conn:
            cc = conn.execute(
                """SELECT cc.*, le.code le_code, le.name le_name, le.id le_id FROM cost_centers cc
                   JOIN legal_entities le ON le.id=cc.legal_entity_id WHERE cc.id=?""", (cc_id,)).fetchone()
            if not cc:
                return self.not_found_page("misc.cc_not_found")
            wbs_rows = wbs_query(conn, "w.cost_center_id=?", (cc_id,))
        wbs_trs = "".join(
            f'<tr><td><a href="/wbs/{w["id"]}">{esc(full_wbs(w["le_code"], w["code"]))}</a></td><td>{esc(w["name"])}</td></tr>'
            for w in wbs_rows) or f'<tr><td colspan="2" class="muted">{esc(self.t("empty.wbs"))}</td></tr>'
        body = f"""<div class="toolbar"><h1>{esc(cc['code'])}: {esc(cc['name'])}</h1><a class="button secondary" href="/cost-centers/{cc_id}/edit">{esc(self.t('btn.edit'))}</a><a class="button secondary" href="/cost-centers">{esc(self.t('btn.back_to_cc'))}</a></div>
        <p class="muted">{esc(self.t('col.legal_entity'))}: <a href="/legal-entities/{cc['le_id']}">{esc(cc['le_code'])} — {esc(cc['le_name'])}</a></p>
        <div class="panel"><h2>{esc(self.t('h2.wbs_of_cc'))}</h2><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.full_wbs'))}</th><th>{esc(self.t('col.name'))}</th></tr></thead><tbody>{wbs_trs}</tbody></table></div></div>"""
        self.send_html(self.page(cc["code"], body))

    def cc_edit_page(self, cc_id):
        with db() as conn:
            cc = conn.execute("SELECT * FROM cost_centers WHERE id=?", (cc_id,)).fetchone()
            if not cc:
                return self.not_found_page("misc.cc_not_found")
            le_opts = self.le_options(conn, cc["legal_entity_id"])
            linked = conn.execute("SELECT COUNT(*) FROM wbs WHERE cost_center_id=?", (cc_id,)).fetchone()[0]
        if linked:
            delete_block = f'<p class="muted small">{esc(self.t("misc.cc_delete_blocked", linked=linked))}</p>'
        else:
            delete_block = (f'<form method="post" action="/cost-centers/{cc_id}/delete">{self.csrf_input()}'
                            f'<button class="danger" type="submit">{esc(self.t("btn.delete_cc"))}</button></form>')
        body = f"""<div class="toolbar"><h1>{self.t('misc.h1_cc_edit', code=esc(cc['code']))}</h1><a class="button secondary" href="/cost-centers/{cc_id}">{esc(self.t('btn.back_to_cc'))}</a></div>
        <div class="panel"><form method="post" action="/cost-centers/{cc_id}/edit">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.code'))} *</label><input name="code" required value="{esc(cc['code'])}"></div>
        <div><label>{esc(self.t('label.name'))} *</label><input name="name" required value="{esc(cc['name'])}"></div>
        <div><label>{esc(self.t('label.legal_entity'))} *</label><select name="legal_entity_id" required>{le_opts}</select></div>
        <div class="full"><button type="submit">{esc(self.t('btn.save_changes'))}</button></div></div></form></div>
        <br><div class="panel"><h2>{esc(self.t('h2.deletion'))}</h2>{delete_block}</div>"""
        self.send_html(self.page(self.t("title.cc_edit"), body))

    def create_cc(self, data):
        code = parse_ref_code(data, "code", self.lang)
        name = require(data, "name", self.t("field.name"), self.lang)
        le_id = parse_int(data.get("legal_entity_id"), self.t("field.entity_code"), self.lang)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with db(write=True) as conn:
            if not conn.execute("SELECT 1 FROM legal_entities WHERE id=?", (le_id,)).fetchone():
                raise ValueError(self.t("error.le_not_found"))
            conn.execute("INSERT INTO cost_centers(code,name,legal_entity_id,created_at) VALUES(?,?,?,?)",
                         (code, name, le_id, now))
        self.redirect("/cost-centers", self.t("flash.cc_created"))

    def update_cc(self, cc_id, data):
        code = parse_ref_code(data, "code", self.lang)
        name = require(data, "name", self.t("field.name"), self.lang)
        le_id = parse_int(data.get("legal_entity_id"), self.t("field.entity_code"), self.lang)
        with db(write=True) as conn:
            if not conn.execute("SELECT 1 FROM cost_centers WHERE id=?", (cc_id,)).fetchone():
                raise ValueError(self.t("error.cc_not_found"))
            if not conn.execute("SELECT 1 FROM legal_entities WHERE id=?", (le_id,)).fetchone():
                raise ValueError(self.t("error.le_not_found"))
            conn.execute("UPDATE cost_centers SET code=?,name=?,legal_entity_id=? WHERE id=?",
                         (code, name, le_id, cc_id))
        self.redirect(f"/cost-centers/{cc_id}", self.t("flash.cc_updated"))

    def delete_cc(self, cc_id, data):
        with db(write=True) as conn:
            if not conn.execute("SELECT 1 FROM cost_centers WHERE id=?", (cc_id,)).fetchone():
                raise ValueError(self.t("error.cc_not_found"))
            if conn.execute("SELECT COUNT(*) FROM wbs WHERE cost_center_id=?", (cc_id,)).fetchone()[0]:
                raise ValueError(self.t("error.cannot_delete_cc_linked"))
            conn.execute("DELETE FROM cost_centers WHERE id=?", (cc_id,))
        self.redirect("/cost-centers", self.t("flash.cc_deleted"))

    # -- WBS ------------------------------------------------------------ #
    def wbs_page(self):
        with db() as conn:
            rows = wbs_query(conn)
            ce_labels = wbs_ce_labels(conn)
            cc_opts = self.cc_options(conn)
        trs = "".join(
            f'<tr><td><a href="/wbs/{w["id"]}"><strong>{esc(w["code"])}</strong></a>'
            f'<div class="small muted">{esc(full_wbs(w["le_code"], w["code"]))}</div></td>'
            f'<td>{esc(w["name"])}</td><td><a href="/cost-centers/{w["cc_id"]}">{esc(w["cc_code"])}</a></td>'
            f'<td><a href="/legal-entities/{w["le_id"]}">{esc(w["le_code"])}</a></td>'
            f'<td class="small">{esc(ce_labels.get(w["id"], "—"))}</td>'
            f'<td><a class="button secondary" href="/wbs/{w["id"]}/edit">{esc(self.t("btn.edit"))}</a></td></tr>'
            for w in rows) or f'<tr><td colspan="6" class="muted">{esc(self.t("empty.wbs"))}</td></tr>'
        body = f"""<div class="toolbar"><h1>{esc(self.t('h1.wbs'))}</h1><a class="button secondary" href="/full-wbs">{esc(self.t('hb.full'))}</a><a class="button secondary" href="/handbooks">{esc(self.t('btn.back_to_handbooks'))}</a></div>
        <div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.wbs'))}</th><th>{esc(self.t('col.name'))}</th><th>{esc(self.t('col.cost_center'))}</th><th>{esc(self.t('col.legal_entity'))}</th><th>{esc(self.t('col.ce_list'))}</th><th>{esc(self.t('col.actions'))}</th></tr></thead><tbody>{trs}</tbody></table></div>
        <br><div class="panel"><h2>{esc(self.t('h2.create_wbs'))}</h2><form method="post" action="/wbs/new">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.code'))} *</label><input name="code" required placeholder="IT/Infr/DC.DCT3srv"></div>
        <div><label>{esc(self.t('label.name'))} *</label><input name="name" required></div>
        <div><label>{esc(self.t('label.cost_center_sel'))} *</label><select name="cost_center_id" required>{cc_opts}</select></div>
        <div class="full"><button type="submit">{esc(self.t('btn.create_wbs'))}</button></div></div></form>
        <p class="muted small">{esc(self.t('misc.wbs_code_hint'))}</p></div>"""
        self.send_html(self.page(self.t("h1.wbs"), body))

    def wbs_detail(self, wbs_id):
        with db() as conn:
            w = wbs_row(conn, wbs_id)
            if not w:
                return self.not_found_page("misc.wbs_not_found")
            ces = conn.execute(
                """SELECT c.* FROM wbs_cost_elements l JOIN cost_elements c ON c.id=l.cost_element_id
                   WHERE l.wbs_id=? ORDER BY c.code""", (wbs_id,)).fetchall()
            budgets = conn.execute(
                "SELECT id,code,name,fiscal_year FROM budget_lines WHERE wbs_id=? ORDER BY code", (wbs_id,)).fetchall()
        ce_trs = "".join(
            f'<tr><td><a href="/cost-elements/{c["id"]}">{esc(c["code"])}</a></td><td>{esc(c["name"])}</td></tr>'
            for c in ces) or f'<tr><td colspan="2" class="muted">{esc(self.t("empty.wbs_ce"))}</td></tr>'
        b_trs = "".join(
            f'<tr><td><a href="/budgets/{b["id"]}">{esc(b["code"])}</a></td><td>{esc(b["name"])}</td><td>{b["fiscal_year"]}</td></tr>'
            for b in budgets) or f'<tr><td colspan="3" class="muted">{esc(self.t("empty.budgets"))}</td></tr>'
        meta = self.t("misc.wbs_meta",
                      full=f'<strong>{esc(full_wbs(w["le_code"], w["code"]))}</strong>',
                      le=f'<a href="/legal-entities/{w["le_id"]}">{esc(w["le_code"])}</a>',
                      cc=f'<a href="/cost-centers/{w["cc_id"]}">{esc(w["cc_code"])}</a>')
        body = f"""<div class="toolbar"><h1>{esc(w['code'])}: {esc(w['name'])}</h1><a class="button secondary" href="/wbs/{wbs_id}/edit">{esc(self.t('btn.edit'))}</a><a class="button secondary" href="/wbs">{esc(self.t('btn.back_to_wbs'))}</a></div>
        <p class="muted">{meta}</p>
        <div class="panel"><h2>{esc(self.t('h2.edit_ce_links'))}</h2><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.code'))}</th><th>{esc(self.t('col.name'))}</th></tr></thead><tbody>{ce_trs}</tbody></table></div></div><br>
        <div class="panel"><h2>{esc(self.t('h2.budgets_of_wbs'))}</h2><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.code'))}</th><th>{esc(self.t('col.name'))}</th><th>{esc(self.t('col.year'))}</th></tr></thead><tbody>{b_trs}</tbody></table></div></div>"""
        self.send_html(self.page(w["code"], body))

    def wbs_edit_page(self, wbs_id):
        with db() as conn:
            w = wbs_row(conn, wbs_id)
            if not w:
                return self.not_found_page("misc.wbs_not_found")
            cc_opts = self.cc_options(conn, w["cost_center_id"])
            boxes = self.ce_checkboxes(conn, wbs_ce_ids(conn, wbs_id))
            linked = conn.execute("SELECT COUNT(*) FROM budget_lines WHERE wbs_id=?", (wbs_id,)).fetchone()[0]
        if linked:
            delete_block = f'<p class="muted small">{esc(self.t("misc.wbs_delete_blocked", linked=linked))}</p>'
            move_warning = f'<p class="warn small">{esc(self.t("misc.wbs_move_warning", linked=linked))}</p>'
        else:
            delete_block = (f'<form method="post" action="/wbs/{wbs_id}/delete">{self.csrf_input()}'
                            f'<button class="danger" type="submit">{esc(self.t("btn.delete_wbs"))}</button></form>')
            move_warning = ""
        body = f"""<div class="toolbar"><h1>{self.t('misc.h1_wbs_edit', code=esc(w['code']))}</h1><a class="button secondary" href="/wbs/{wbs_id}">{esc(self.t('btn.back_to_wbs'))}</a></div>
        <div class="panel"><form method="post" action="/wbs/{wbs_id}/edit">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.code'))} *</label><input name="code" required value="{esc(w['code'])}"></div>
        <div><label>{esc(self.t('label.name'))} *</label><input name="name" required value="{esc(w['name'])}"></div>
        <div><label>{esc(self.t('label.cost_center_sel'))} *</label><select name="cost_center_id" required>{cc_opts}</select></div>
        <div class="full"><label>{esc(self.t('label.cost_elements'))}</label>{boxes}</div>
        <div class="full"><button type="submit">{esc(self.t('btn.save_changes'))}</button></div></div></form>
        <p class="muted small">{esc(self.t('misc.wbs_code_hint'))}</p>{move_warning}</div>
        <br><div class="panel"><h2>{esc(self.t('h2.deletion'))}</h2>{delete_block}</div>"""
        self.send_html(self.page(self.t("title.wbs_edit"), body))

    def create_wbs(self, data):
        code = parse_wbs_code(data, "code", self.lang)
        name = require(data, "name", self.t("field.name"), self.lang)
        cc_id = parse_int(data.get("cost_center_id"), self.t("field.code"), self.lang)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with db(write=True) as conn:
            if not conn.execute("SELECT 1 FROM cost_centers WHERE id=?", (cc_id,)).fetchone():
                raise ValueError(self.t("error.cc_not_found"))
            conn.execute("INSERT INTO wbs(code,name,cost_center_id,created_at) VALUES(?,?,?,?)",
                         (code, name, cc_id, now))
        self.redirect("/wbs", self.t("flash.wbs_created"))

    def update_wbs(self, wbs_id, data):
        code = parse_wbs_code(data, "code", self.lang)
        name = require(data, "name", self.t("field.name"), self.lang)
        cc_id = parse_int(data.get("cost_center_id"), self.t("field.code"), self.lang)
        with db(write=True) as conn:
            if not conn.execute("SELECT 1 FROM wbs WHERE id=?", (wbs_id,)).fetchone():
                raise ValueError(self.t("error.wbs_not_found"))
            if not conn.execute("SELECT 1 FROM cost_centers WHERE id=?", (cc_id,)).fetchone():
                raise ValueError(self.t("error.cc_not_found"))
            conn.execute("UPDATE wbs SET code=?,name=?,cost_center_id=? WHERE id=?", (code, name, cc_id, wbs_id))
            # Rewrite the cost-element links from the checkbox set.
            keep = self.selected_ce_ids(conn, data)
            conn.execute("DELETE FROM wbs_cost_elements WHERE wbs_id=?", (wbs_id,))
            conn.executemany("INSERT INTO wbs_cost_elements(wbs_id,cost_element_id) VALUES(?,?)",
                             [(wbs_id, ce) for ce in keep])
            # A budget line may now point at a cost element this WBS no longer
            # carries; clear those rather than leave an invalid pairing behind.
            conn.execute(
                """UPDATE budget_lines SET cost_element_id=NULL WHERE wbs_id=? AND cost_element_id IS NOT NULL
                   AND cost_element_id NOT IN (SELECT cost_element_id FROM wbs_cost_elements WHERE wbs_id=?)""",
                (wbs_id, wbs_id))
        self.redirect(f"/wbs/{wbs_id}", self.t("flash.wbs_updated"))

    def delete_wbs(self, wbs_id, data):
        with db(write=True) as conn:
            if not conn.execute("SELECT 1 FROM wbs WHERE id=?", (wbs_id,)).fetchone():
                raise ValueError(self.t("error.wbs_not_found"))
            if conn.execute("SELECT COUNT(*) FROM budget_lines WHERE wbs_id=?", (wbs_id,)).fetchone()[0]:
                raise ValueError(self.t("error.cannot_delete_wbs_linked"))
            # Cost-element links are attached data, not documents: they never
            # block the delete and go with the record, like a monthly plan.
            conn.execute("DELETE FROM wbs_cost_elements WHERE wbs_id=?", (wbs_id,))
            conn.execute("DELETE FROM wbs WHERE id=?", (wbs_id,))
        self.redirect("/wbs", self.t("flash.wbs_deleted"))

    # -- Cost elements -------------------------------------------------- #
    def ce_page(self):
        with db() as conn:
            rows = conn.execute(
                """SELECT c.*, (SELECT COUNT(*) FROM wbs_cost_elements l WHERE l.cost_element_id=c.id) wbs_count
                   FROM cost_elements c ORDER BY c.code"""
            ).fetchall()
        trs = "".join(
            f'<tr><td><a href="/cost-elements/{r["id"]}"><strong>{esc(r["code"])}</strong></a></td>'
            f'<td>{esc(r["name"])}</td><td>{r["wbs_count"]}</td>'
            f'<td><a class="button secondary" href="/cost-elements/{r["id"]}/edit">{esc(self.t("btn.edit"))}</a></td></tr>'
            for r in rows) or f'<tr><td colspan="4" class="muted">{esc(self.t("empty.cost_elements"))}</td></tr>'
        body = f"""<div class="toolbar"><h1>{esc(self.t('h1.cost_elements'))}</h1><a class="button secondary" href="/handbooks">{esc(self.t('btn.back_to_handbooks'))}</a></div>
        <div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.code'))}</th><th>{esc(self.t('col.name'))}</th><th>{esc(self.t('col.wbs_count'))}</th><th>{esc(self.t('col.actions'))}</th></tr></thead><tbody>{trs}</tbody></table></div>
        <br><div class="panel"><h2>{esc(self.t('h2.create_ce'))}</h2><form method="post" action="/cost-elements/new">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.code'))} *</label><input name="code" required placeholder="6100100"></div>
        <div><label>{esc(self.t('label.name'))} *</label><input name="name" required></div>
        <div class="full"><button type="submit">{esc(self.t('btn.create_ce'))}</button></div></div></form></div>"""
        self.send_html(self.page(self.t("h1.cost_elements"), body))

    def ce_detail(self, ce_id):
        with db() as conn:
            ce = conn.execute("SELECT * FROM cost_elements WHERE id=?", (ce_id,)).fetchone()
            if not ce:
                return self.not_found_page("misc.ce_not_found")
            rows = wbs_query(conn, "w.id IN (SELECT wbs_id FROM wbs_cost_elements WHERE cost_element_id=?)", (ce_id,))
        trs = "".join(
            f'<tr><td><a href="/wbs/{w["id"]}">{esc(full_wbs(w["le_code"], w["code"]))}</a></td><td>{esc(w["name"])}</td></tr>'
            for w in rows) or f'<tr><td colspan="2" class="muted">{esc(self.t("empty.wbs"))}</td></tr>'
        body = f"""<div class="toolbar"><h1>{esc(ce['code'])}: {esc(ce['name'])}</h1><a class="button secondary" href="/cost-elements/{ce_id}/edit">{esc(self.t('btn.edit'))}</a><a class="button secondary" href="/cost-elements">{esc(self.t('btn.back_to_ce'))}</a></div>
        <div class="panel"><h2>{esc(self.t('h2.wbs_of_ce'))}</h2><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.full_wbs'))}</th><th>{esc(self.t('col.name'))}</th></tr></thead><tbody>{trs}</tbody></table></div></div>"""
        self.send_html(self.page(ce["code"], body))

    def ce_edit_page(self, ce_id):
        with db() as conn:
            ce = conn.execute("SELECT * FROM cost_elements WHERE id=?", (ce_id,)).fetchone()
            if not ce:
                return self.not_found_page("misc.ce_not_found")
            linked = conn.execute(
                """SELECT (SELECT COUNT(*) FROM wbs_cost_elements WHERE cost_element_id=?)
                        + (SELECT COUNT(*) FROM budget_lines WHERE cost_element_id=?)""",
                (ce_id, ce_id)).fetchone()[0]
        if linked:
            delete_block = f'<p class="muted small">{esc(self.t("misc.ce_delete_blocked", linked=linked))}</p>'
        else:
            delete_block = (f'<form method="post" action="/cost-elements/{ce_id}/delete">{self.csrf_input()}'
                            f'<button class="danger" type="submit">{esc(self.t("btn.delete_ce"))}</button></form>')
        body = f"""<div class="toolbar"><h1>{self.t('misc.h1_ce_edit', code=esc(ce['code']))}</h1><a class="button secondary" href="/cost-elements/{ce_id}">{esc(self.t('btn.back_to_ce'))}</a></div>
        <div class="panel"><form method="post" action="/cost-elements/{ce_id}/edit">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.code'))} *</label><input name="code" required value="{esc(ce['code'])}"></div>
        <div><label>{esc(self.t('label.name'))} *</label><input name="name" required value="{esc(ce['name'])}"></div>
        <div class="full"><button type="submit">{esc(self.t('btn.save_changes'))}</button></div></div></form></div>
        <br><div class="panel"><h2>{esc(self.t('h2.deletion'))}</h2>{delete_block}</div>"""
        self.send_html(self.page(self.t("title.ce_edit"), body))

    def create_ce(self, data):
        code = parse_ref_code(data, "code", self.lang)
        name = require(data, "name", self.t("field.name"), self.lang)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with db(write=True) as conn:
            conn.execute("INSERT INTO cost_elements(code,name,created_at) VALUES(?,?,?)", (code, name, now))
        self.redirect("/cost-elements", self.t("flash.ce_created"))

    def update_ce(self, ce_id, data):
        code = parse_ref_code(data, "code", self.lang)
        name = require(data, "name", self.t("field.name"), self.lang)
        with db(write=True) as conn:
            if not conn.execute("SELECT 1 FROM cost_elements WHERE id=?", (ce_id,)).fetchone():
                raise ValueError(self.t("error.ce_not_found"))
            conn.execute("UPDATE cost_elements SET code=?,name=? WHERE id=?", (code, name, ce_id))
        self.redirect(f"/cost-elements/{ce_id}", self.t("flash.ce_updated"))

    def delete_ce(self, ce_id, data):
        with db(write=True) as conn:
            if not conn.execute("SELECT 1 FROM cost_elements WHERE id=?", (ce_id,)).fetchone():
                raise ValueError(self.t("error.ce_not_found"))
            linked = conn.execute(
                """SELECT (SELECT COUNT(*) FROM wbs_cost_elements WHERE cost_element_id=?)
                        + (SELECT COUNT(*) FROM budget_lines WHERE cost_element_id=?)""",
                (ce_id, ce_id)).fetchone()[0]
            if linked:
                raise ValueError(self.t("error.cannot_delete_ce_linked"))
            conn.execute("DELETE FROM cost_elements WHERE id=?", (ce_id,))
        self.redirect("/cost-elements", self.t("flash.ce_deleted"))

    # -- Full WBS register (computed, no table of its own) --------------- #
    def full_wbs_page(self, query):
        le_filter = (query.get("le", [""])[0] or "").strip()
        cc_filter = (query.get("cc", [""])[0] or "").strip()
        # Unparsable filters are dropped rather than rejected, the same way the
        # month filter on the budget page treats junk input.
        where, params = [], []
        if re.fullmatch(r"\d+", le_filter):
            where.append("le.id=?")
            params.append(int(le_filter))
        else:
            le_filter = ""
        if re.fullmatch(r"\d+", cc_filter):
            where.append("cc.id=?")
            params.append(int(cc_filter))
        else:
            cc_filter = ""
        with db() as conn:
            rows = wbs_query(conn, " AND ".join(where), tuple(params))
            ce_labels = wbs_ce_labels(conn)
            budget_counts = {r["wbs_id"]: r["n"] for r in conn.execute(
                "SELECT wbs_id, COUNT(*) n FROM budget_lines WHERE wbs_id IS NOT NULL GROUP BY wbs_id")}
            le_opts = "".join(
                f'<option value="{r["id"]}"{" selected" if str(r["id"]) == le_filter else ""}>{esc(r["code"])}</option>'
                for r in conn.execute("SELECT id,code FROM legal_entities ORDER BY code"))
            cc_opts = "".join(
                f'<option value="{r["id"]}"{" selected" if str(r["id"]) == cc_filter else ""}>{esc(r["code"])}</option>'
                for r in conn.execute("SELECT id,code FROM cost_centers ORDER BY code"))
        trs = "".join(
            f'<tr><td><a href="/wbs/{w["id"]}"><strong>{esc(full_wbs(w["le_code"], w["code"]))}</strong></a></td>'
            f'<td>{esc(w["name"])}</td><td><a href="/legal-entities/{w["le_id"]}">{esc(w["le_code"])}</a></td>'
            f'<td><a href="/cost-centers/{w["cc_id"]}">{esc(w["cc_code"])}</a></td>'
            f'<td class="small">{esc(ce_labels.get(w["id"], "—"))}</td>'
            f'<td>{budget_counts.get(w["id"], 0)}</td>'
            f'<td><a class="button secondary" href="/wbs/{w["id"]}/edit">{esc(self.t("btn.edit"))}</a></td></tr>'
            for w in rows) or f'<tr><td colspan="7" class="muted">{esc(self.t("empty.full_wbs"))}</td></tr>'
        body = f"""<div class="toolbar"><h1>{esc(self.t('h1.full_wbs'))}</h1><a class="button secondary" href="/wbs">{esc(self.t('btn.back_to_wbs'))}</a><a class="button secondary" href="/handbooks">{esc(self.t('btn.back_to_handbooks'))}</a></div>
        <div class="panel"><form method="get" action="/full-wbs"><div class="form-grid">
        <div><label>{esc(self.t('filter.legal_entity'))}</label><select name="le"><option value="">{esc(self.t('filter.all'))}</option>{le_opts}</select></div>
        <div><label>{esc(self.t('filter.cost_center'))}</label><select name="cc"><option value="">{esc(self.t('filter.all'))}</option>{cc_opts}</select></div>
        <div class="full"><button type="submit">{esc(self.t('btn.apply_filter'))}</button> <a class="button secondary" href="/full-wbs">{esc(self.t('btn.clear_filter'))}</a></div>
        </div></form></div><br>
        <div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.full_wbs'))}</th><th>{esc(self.t('col.name'))}</th><th>{esc(self.t('col.legal_entity'))}</th><th>{esc(self.t('col.cost_center'))}</th><th>{esc(self.t('col.ce_list'))}</th><th>{esc(self.t('col.budgets'))}</th><th>{esc(self.t('col.actions'))}</th></tr></thead><tbody>{trs}</tbody></table></div>
        <p class="muted small">{esc(self.t('misc.full_wbs_note'))}</p>"""
        self.send_html(self.page(self.t("h1.full_wbs"), body))

    # ------------------------------------------------------------------ #
    # Settings: currencies, base currency and CBR rate refresh           #
    # ------------------------------------------------------------------ #
    def settings_page(self):
        with db() as conn:
            currencies = conn.execute(
                "SELECT code,name,rate_micro,is_active,updated_at FROM currencies ORDER BY is_active DESC, code"
            ).fetchall()
            base = get_setting(conn, "base_currency", "RUB")
            rates_updated = get_setting(conn, "rates_updated_at")
        active_codes = [c["code"] for c in currencies if c["is_active"]] or [base]
        base_options = "".join(
            f'<option value="{esc(c)}"{" selected" if c == base else ""}>{esc(c)}</option>'
            for c in active_codes)
        cur_rows = ""
        for c in currencies:
            if c["rate_micro"] is None:
                rate = '<span class="muted">—</span>'
            else:
                rate = esc(f'{Decimal(c["rate_micro"]) / RUB_MICRO:.4f}')
            checked = " checked" if c["is_active"] else ""
            cur_rows += (f'<tr><td><strong>{esc(c["code"])}</strong></td><td>{esc(c["name"])}</td>'
                         f'<td>{rate}</td><td class="small muted">{esc(c["updated_at"] or "—")}</td>'
                         f'<td><input type="checkbox" name="active_{esc(c["code"])}" value="1"{checked}></td></tr>')
        updated_line = (esc(self.t("misc.rates_updated_at", when=rates_updated)) if rates_updated
                        else esc(self.t("misc.rates_never")))
        body = f"""<div class="toolbar"><h1>{esc(self.t('h1.settings'))}</h1></div>
        <form method="post" action="/settings">{self.csrf_input()}
        <div class="panel"><h2>{esc(self.t('h2.base_currency'))}</h2>
        <div class="form-grid"><div><label>{esc(self.t('label.base_currency'))}</label>
        <select name="base_currency">{base_options}</select></div></div>
        <p class="muted small">{esc(self.t('misc.base_currency_hint'))}</p></div><br>
        <div class="panel"><h2>{esc(self.t('h2.currencies'))}</h2><div class="table-wrap"><table><thead><tr>
        <th>{esc(self.t('col.currency'))}</th><th>{esc(self.t('col.name'))}</th><th>{esc(self.t('col.rate'))}</th>
        <th>{esc(self.t('col.rate_updated'))}</th><th>{esc(self.t('col.active'))}</th></tr></thead>
        <tbody>{cur_rows}</tbody></table></div>
        <br><button type="submit">{esc(self.t('btn.save_settings'))}</button></div></form><br>
        <div class="panel"><h2>{esc(self.t('h2.cbr_rates'))}</h2>
        <p class="muted small">{updated_line}</p>
        <form method="post" action="/settings/refresh-rates">{self.csrf_input()}
        <button type="submit">{esc(self.t('btn.refresh_rates'))}</button></form></div>"""
        self.send_html(self.page(self.t('h1.settings'), body))

    def save_settings(self, data):
        base = (data.get("base_currency") or "").strip().upper()
        with db(write=True) as conn:
            all_codes = [r["code"] for r in conn.execute("SELECT code FROM currencies")]
            if base not in all_codes:
                raise ValueError(self.t("error.base_currency_unknown"))
            active = {code for code in all_codes if data.get(f"active_{code}") == "1"}
            active.add(base)  # the base display currency must always stay active
            for code in all_codes:
                conn.execute("UPDATE currencies SET is_active=? WHERE code=?",
                             (1 if code in active else 0, code))
            set_setting(conn, "base_currency", base)
        self.redirect("/settings", self.t("flash.settings_saved"))

    def refresh_rates_action(self, data):
        # Fetch from the CBR before opening the write transaction so a slow or
        # failing network call never holds the DB write lock.
        try:
            rates = fetch_cbr_rates()
        except ValueError as exc:
            raise ValueError(self.t("error.cbr_fetch", detail=str(exc)))
        with db(write=True) as conn:
            count = refresh_rates(conn, fetch=lambda: rates)
        self.redirect("/settings", self.t("flash.rates_refreshed", count=count))

    def api_summary(self):
        with db() as conn:
            metrics = all_budget_metrics(conn)
            wbs_by_id = wbs_index(conn)
            ce_by_id = ce_index(conn)
        payload = []
        for m in metrics:
            r=m["row"]
            w = wbs_by_id.get(r["wbs_id"])
            payload.append({
                "id":r["id"],"code":r["code"],"name":r["name"],"fiscal_year":r["fiscal_year"],
                "currency":r["currency"],"holder":r["holder_name"],
                "legal_entity":w["le_code"] if w else None,"cost_center":w["cc_code"] if w else None,
                "wbs":w["code"] if w else None,
                "full_wbs":full_wbs(w["le_code"], w["code"]) if w else None,
                "cost_element":ce_by_id.get(r["cost_element_id"]),
                "approved_cents":m["approved"],
                "released_cents":m["released"],"actuals_cents":m["actuals"],"commitments_cents":m["commitments"],"available_cents":m["available"]
            })
        self.send_json({"budgets": payload})


def ensure_writable(path, label):
    """Fail fast with an actionable message rather than a stack trace.

    A volume attached by the hosting platform arrives owned by root more often
    than not, and "unable to open database file" several frames deep is a poor
    way to learn that.
    """
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise SystemExit(f"cannot create {label} {path!r}: {exc}")
    if not os.access(path, os.W_OK | os.X_OK):
        try:
            info = os.stat(path)
            owner = f"owned by uid={info.st_uid} gid={info.st_gid}, mode {info.st_mode & 0o7777:04o}"
        except OSError as exc:
            owner = f"could not be inspected: {exc}"
        raise SystemExit(
            f"{label} {path!r} is not writable by uid={os.getuid()} gid={os.getgid()}; "
            f"it is {owner}. Either start the container as root so the entrypoint can "
            f"take ownership (it drops privileges again before this point, and needs "
            f"the CHOWN capability to do so), have the platform mount it writable for "
            f"that account, or point {label} at a writable path."
        )


def apply_data_file_mode():
    """Relax the database files to DATA_FILE_MODE so a later container running
    under a different uid in the same group can still open them. Failure is not
    fatal: another account may own the files, and being unable to widen the
    mode is only a problem if the uid actually changes."""
    for suffix in ("", "-wal", "-shm"):
        target = DB_PATH + suffix
        try:
            os.chmod(target, DATA_FILE_MODE)
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(f"warning: cannot set mode {DATA_FILE_MODE:04o} on {target!r}: {exc}")


def main():
    ensure_writable(DATA_DIR, "DATA_DIR")
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir != os.path.abspath(DATA_DIR):
        ensure_writable(db_dir, "DB_PATH directory")
    init_db()
    apply_data_file_mode()
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)

    def shutdown(signum, _frame):
        # shutdown() blocks until serve_forever() returns, and signal handlers
        # run on the thread already sitting in serve_forever(), so calling it
        # inline would deadlock. Hand it to a helper thread instead.
        print(f"{APP_NAME} received signal {signum}, shutting down")
        threading.Thread(target=server.shutdown, daemon=True).start()

    # As PID 1 (the container entrypoint) a process gets no default action for
    # signals it has not handled, so without these SIGTERM is ignored outright:
    # `docker stop` would stall for its full timeout and then SIGKILL us in the
    # middle of a SQLite write.
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"{APP_NAME} listening on http://{HOST}:{PORT}; DB={DB_PATH}")
    server.serve_forever()
    server.server_close()


if __name__ == "__main__":
    main()
