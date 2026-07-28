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
        "misc.footer": "MVP. Все суммы хранятся в decimal с двумя знаками; операции сохраняются в журнале.",  # page footer note

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
        "label.cost_center": "Cost Center",          # cost center
        "label.wbs": "WBS",                          # WBS element
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
        "misc.budget_meta": "Budget Holder: {holder} · Cost Center: {cost_center} · WBS: {wbs} · CE: {ce}",  # budget detail meta line
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
        "misc.footer": "MVP. All amounts are stored as two-decimal values; operations are kept in an audit log.",

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
        "label.cost_center": "Cost Center",
        "label.wbs": "WBS",
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
        "misc.budget_meta": "Budget Holder: {holder} · Cost Center: {cost_center} · WBS: {wbs} · CE: {ce}",
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

# Reference catalogs (справочники) and WBS coding strings. Same pattern as the
# blocks above: the feature contributes its own keys without editing the large
# catalog, and both language blocks carry the same set of keys.
_REFERENCE_TRANSLATIONS = {
    "ru": {
        # -- navigation & headings ----------------------------------------- #
        "nav.references": "Справочники",                # top nav — reference catalogs
        "nav.wbs": "WBS",                               # top nav — WBS elements
        "h1.references": "Справочники",                 # references index title
        "h1.wbs": "WBS-элементы",                       # WBS list title
        "h2.create_record": "Добавить запись",          # reference — create form
        "h2.edit_record": "Редактировать запись",       # reference — edit form
        "h2.create_wbs": "Создать WBS-элемент",         # WBS — create form
        "h2.edit_wbs": "Редактировать WBS-элемент",     # WBS — edit form
        "h2.wbs_coding": "Кодирование WBS",             # settings — WBS block

        # -- catalog names -------------------------------------------------- #
        "ref.functions": "Функции",                     # catalog: functions
        "ref.sub_functions": "Субфункции",              # catalog: sub-functions
        "ref.projects": "Проекты",                      # catalog: projects
        "ref.cost_centers": "Cost Centers",             # catalog: cost centers
        "ref.cost_elements": "Cost Elements",           # catalog: cost elements
        "ref.holders": "Budget Holders",                # catalog: budget holders
        "ref.vendors": "Поставщики",                    # catalog: vendors
        "ref.currencies": "Валюты",                     # catalog: currencies (on /settings)
        "ref.wbs": "WBS-элементы",                      # catalog: WBS elements

        # -- table columns & labels ----------------------------------------- #
        "col.records": "Записей",                       # record count column
        "col.function": "Функция",                      # function column
        "col.sub_function": "Субфункция",               # sub-function column
        "col.project": "Проект",                        # project column
        "col.extension": "Extension",                   # WBS extension column
        "label.function": "Функция",                    # function selector
        "label.sub_function": "Субфункция",             # sub-function selector
        "label.project": "Проект",                      # project selector
        "label.extension": "Extension",                 # extension input
        "label.wbs_element": "WBS-элемент",             # WBS selector on the budget form
        "label.wbs_prefix": "Префикс WBS",              # WBS prefix setting
        "label.active": "Активна",                      # is-active checkbox

        # -- buttons -------------------------------------------------------- #
        "btn.create_record": "Добавить",                # submit new reference record
        "btn.delete_record": "Удалить запись",          # delete reference record
        "btn.back_to_references": "К справочникам",     # back to references index
        "btn.back_to_wbs": "К WBS",                     # back to WBS list
        "btn.create_wbs": "Создать WBS-элемент",        # submit new WBS element
        "btn.delete_wbs": "Удалить WBS-элемент",        # delete WBS element
        "btn.create_budget": "Создать бюджет",          # create a budget for a WBS element

        # -- hints ----------------------------------------------------------- #
        "hint.function_code": "Ровно 2 знака: A–Z, 0–9.",   # function code rule
        "hint.subfunction_code": "Ровно 4 знака: A–Z, 0–9.",  # sub-function code rule
        "hint.project_code": "До {max} знаков: A–Z, 0–9, дефис.",  # project code rule
        "hint.code_generic": "A–Z, 0–9, дефис.",            # generic code rule
        "hint.extension": "Необязательно. Проект и extension вместе — не более {max} знаков.",  # extension rule
        "hint.wbs_prefix": "Код, с которого начинается полный WBS. Может быть пустым. При изменении коды всех WBS-элементов пересобираются.",  # prefix hint

        # -- misc inline text ------------------------------------------------ #
        "misc.wbs_format": "Формат: {example}. Функция — 2 знака, субфункция — 4 знака, проект и extension вместе — не более {max} знаков.",  # WBS format note
        "misc.wbs_no_budget": "Бюджет не создан",           # WBS without a budget
        "misc.wbs_without_budget": "WBS-элементов без бюджета: {count}. Бюджет должен быть создан для каждого.",  # warning banner
        "misc.all_wbs_have_budget": "Для всех WBS-элементов создан бюджет.",  # all-good note
        "misc.no_free_wbs": "Свободных WBS-элементов нет: сначала создайте WBS-элемент.",  # empty WBS selector
        "misc.ref_delete_blocked": "Удаление недоступно: запись используется ({count}).",  # reference in use
        "misc.wbs_delete_blocked": "Удаление недоступно: по WBS-элементу заведён бюджет.",  # WBS in use
        "misc.inactive": "неактивна",                       # inactive marker
        "misc.h1_reference": "Справочник «{title}»",        # reference page heading
        "misc.h1_wbs": "WBS {code}",                        # WBS detail heading

        # -- empty-table placeholders ---------------------------------------- #
        "empty.records": "Записей нет",                     # no reference records
        "empty.wbs": "WBS-элементы отсутствуют",            # no WBS elements

        # -- flash messages --------------------------------------------------- #
        "flash.record_created": "Запись добавлена",         # reference record created
        "flash.record_updated": "Запись обновлена",         # reference record updated
        "flash.record_deleted": "Запись удалена",           # reference record deleted
        "flash.wbs_created": "WBS-элемент создан",          # WBS created
        "flash.wbs_updated": "WBS-элемент обновлён",        # WBS updated
        "flash.wbs_deleted": "WBS-элемент удалён",          # WBS deleted

        # -- errors ------------------------------------------------------------ #
        "error.wbs_function_len": "Код функции должен содержать ровно 2 знака",   # function length
        "error.wbs_subfunction_len": "Код субфункции должен содержать ровно 4 знака",  # sub-function length
        "error.wbs_tail_len": "Проект и extension вместе не должны превышать {max} знаков",  # levels 3+4 length
        "error.wbs_charset": "Допустимы только латинские буквы, цифры и дефис",   # charset
        "error.wbs_exists": "Такой WBS уже существует",                          # duplicate WBS
        "error.subfunction_not_in_function": "Субфункция не принадлежит выбранной функции",  # mismatch
        "error.ref_not_found": "Запись справочника не найдена",                   # missing record
        "error.ref_in_use": "Запись используется и не может быть удалена",        # delete blocked
        "error.code_exists": "Код уже занят",                                     # duplicate code
        "error.bad_reference": "Неизвестный справочник",                          # unknown catalog slug
        "error.wbs_not_found": "WBS-элемент не найден",                           # missing WBS
        "error.wbs_taken": "По этому WBS-элементу уже заведён бюджет",            # WBS already budgeted
        "error.wbs_delete_has_budget": "Нельзя удалить WBS-элемент, по которому заведён бюджет",  # WBS delete blocked
        "error.record_inactive": "Запись справочника неактивна",                  # inactive record chosen

        # -- field names embedded into error messages -------------------------- #
        "field.function": "Функция",                     # function
        "field.sub_function": "Субфункция",              # sub-function
        "field.project": "Проект",                       # project
        "field.wbs_element": "WBS-элемент",              # WBS element
        "field.record_code": "Код",                      # reference code
        "field.record_name": "Название",                 # reference name
    },
    "en": {
        # navigation & headings
        "nav.references": "References",
        "nav.wbs": "WBS",
        "h1.references": "Reference catalogs",
        "h1.wbs": "WBS elements",
        "h2.create_record": "Add record",
        "h2.edit_record": "Edit record",
        "h2.create_wbs": "Create WBS element",
        "h2.edit_wbs": "Edit WBS element",
        "h2.wbs_coding": "WBS coding",

        # catalog names
        "ref.functions": "Functions",
        "ref.sub_functions": "Sub-functions",
        "ref.projects": "Projects",
        "ref.cost_centers": "Cost Centers",
        "ref.cost_elements": "Cost Elements",
        "ref.holders": "Budget Holders",
        "ref.vendors": "Vendors",
        "ref.currencies": "Currencies",
        "ref.wbs": "WBS elements",

        # table columns & labels
        "col.records": "Records",
        "col.function": "Function",
        "col.sub_function": "Sub-function",
        "col.project": "Project",
        "col.extension": "Extension",
        "label.function": "Function",
        "label.sub_function": "Sub-function",
        "label.project": "Project",
        "label.extension": "Extension",
        "label.wbs_element": "WBS element",
        "label.wbs_prefix": "WBS prefix",
        "label.active": "Active",

        # buttons
        "btn.create_record": "Add",
        "btn.delete_record": "Delete record",
        "btn.back_to_references": "Back to references",
        "btn.back_to_wbs": "Back to WBS",
        "btn.create_wbs": "Create WBS element",
        "btn.delete_wbs": "Delete WBS element",
        "btn.create_budget": "Create budget",

        # hints
        "hint.function_code": "Exactly 2 characters: A–Z, 0–9.",
        "hint.subfunction_code": "Exactly 4 characters: A–Z, 0–9.",
        "hint.project_code": "Up to {max} characters: A–Z, 0–9, hyphen.",
        "hint.code_generic": "A–Z, 0–9, hyphen.",
        "hint.extension": "Optional. Project and extension together must not exceed {max} characters.",
        "hint.wbs_prefix": "The code every full WBS starts with. May be empty. Changing it rebuilds every WBS element code.",

        # misc inline text
        "misc.wbs_format": "Format: {example}. Function is 2 characters, sub-function 4, and project plus extension at most {max} characters in total.",
        "misc.wbs_no_budget": "No budget",
        "misc.wbs_without_budget": "WBS elements without a budget: {count}. A budget must be created for each of them.",
        "misc.all_wbs_have_budget": "Every WBS element has a budget.",
        "misc.no_free_wbs": "No free WBS elements: create a WBS element first.",
        "misc.ref_delete_blocked": "Deletion unavailable: the record is in use ({count}).",
        "misc.wbs_delete_blocked": "Deletion unavailable: a budget is attached to this WBS element.",
        "misc.inactive": "inactive",
        "misc.h1_reference": "Catalog “{title}”",
        "misc.h1_wbs": "WBS {code}",

        # empty-table placeholders
        "empty.records": "No records",
        "empty.wbs": "No WBS elements",

        # flash messages
        "flash.record_created": "Record added",
        "flash.record_updated": "Record updated",
        "flash.record_deleted": "Record deleted",
        "flash.wbs_created": "WBS element created",
        "flash.wbs_updated": "WBS element updated",
        "flash.wbs_deleted": "WBS element deleted",

        # errors
        "error.wbs_function_len": "The function code must be exactly 2 characters",
        "error.wbs_subfunction_len": "The sub-function code must be exactly 4 characters",
        "error.wbs_tail_len": "Project and extension together must not exceed {max} characters",
        "error.wbs_charset": "Only Latin letters, digits and hyphens are allowed",
        "error.wbs_exists": "This WBS already exists",
        "error.subfunction_not_in_function": "The sub-function does not belong to the selected function",
        "error.ref_not_found": "Reference record not found",
        "error.ref_in_use": "The record is in use and cannot be deleted",
        "error.code_exists": "The code is already taken",
        "error.bad_reference": "Unknown reference catalog",
        "error.wbs_not_found": "WBS element not found",
        "error.wbs_taken": "This WBS element already has a budget",
        "error.wbs_delete_has_budget": "Cannot delete a WBS element that has a budget",
        "error.record_inactive": "The reference record is not active",

        # field names embedded into error messages
        "field.function": "Function",
        "field.sub_function": "Sub-function",
        "field.project": "Project",
        "field.wbs_element": "WBS element",
        "field.record_code": "Code",
        "field.record_name": "Name",
    },
}
for _lang, _msgs in _REFERENCE_TRANSLATIONS.items():
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


# --------------------------------------------------------------------------- #
# Money                                                                        #
# --------------------------------------------------------------------------- #
# Every monetary value is a Decimal carrying exactly two decimals, in memory
# and in the database alike: there is no "amount in minor units" integer field
# anywhere. SQLite has no decimal type, so money columns are declared
# NUMERIC(18,2) and Decimals are adapted to their canonical text form on the
# way in; dec() restores them on the way out and dsum() keeps SQL summation
# exact.
MONEY_Q = Decimal("0.01")        # scale of every money value
RATE_Q = Decimal("0.000001")     # scale of an exchange rate
ZERO = Decimal("0.00")

sqlite3.register_adapter(Decimal, str)


def dec(value, q=MONEY_Q):
    """Return `value` as a Decimal quantized to `q`.

    Accepts everything SQLite hands back for a NUMERIC column — INTEGER when
    the stored value is whole, REAL otherwise, TEXT from dsum() — plus Decimal,
    str and None. repr() of such a float round-trips exactly at this scale, so
    quantizing here restores the stored amount without float drift. (REAL keeps
    ~15 significant digits, which covers every two-decimal amount below 10^13 —
    far beyond any budget this app is meant to hold.)
    """
    if value is None:
        return Decimal(0).quantize(q)
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, float):
        d = Decimal(repr(value))
    else:
        d = Decimal(str(value))
    return d.quantize(q, rounding=ROUND_HALF_UP)


class DecimalSum:
    """Exact SUM() over a money column, registered below as dsum().

    SQLite's own SUM() would add the REAL representations and drift by
    fractions of a cent; this accumulates Decimals instead and returns the
    canonical two-decimal text that dec() reads back exactly. An empty group
    yields "0.00", so callers need no COALESCE.
    """

    def __init__(self):
        self.total = ZERO

    def step(self, value):
        if value is not None:
            self.total += dec(value)

    def finalize(self):
        return str(self.total)


def utcnow():
    """Current UTC timestamp in the compact ISO form stored in created_at."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@contextlib.contextmanager
def db(write=False):
    # write=True opens a BEGIN IMMEDIATE transaction so that a read-check
    # followed by a write is atomic against other writers. Without it two
    # concurrent requests could both pass an "available budget" check and
    # both commit, overspending the budget (TOCTOU race).
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.create_aggregate("dsum", 1, DecimalSum)
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


# --------------------------------------------------------------------------- #
# Reference catalogs and WBS coding                                            #
# --------------------------------------------------------------------------- #
# Budget Holder, Vendor, Cost Center, Cost Element and the three WBS levels are
# reference data: each lives in its own catalog table and documents point at an
# entry by id instead of repeating free text.
#
# A WBS element is assembled from those catalogs, and its full code follows the
# coding standard:
#
#     <prefix><Function>/<Sub-function>/<Project>[.<extension>]
#               2 chars      4 chars     levels 3+4: 15 chars in total
#
# The full WBS starts with the prefix code kept in app_settings ("wbs_prefix",
# empty by default and editable on /settings). A WBS is unique and carries
# exactly one budget line, so a budget can be formed for every element.
WBS_FUNCTION_LEN = 2
WBS_SUBFUNCTION_LEN = 4
WBS_TAIL_MAX = 15              # project + extension together (levels 3 and 4)
# Placeholder levels for WBS text that predates the coding standard.
LEGACY_FUNCTION_CODE = "ZZ"
LEGACY_SUBFUNCTION_CODE = "ZZZZ"


def normalize_ref_code(value, limit=32):
    """Uppercase `value` and keep only the characters a code may contain.

    Anything else — spaces, punctuation, Cyrillic — collapses into a single
    hyphen, so free text such as "IT Operations" yields a usable "IT-OPERATIONS".
    """
    text = re.sub(r"[^A-Z0-9]+", "-", (value or "").strip().upper()).strip("-")
    return text[:limit]


def build_wbs(prefix, function_code, sub_code, project_code, extension=""):
    """Assemble the full WBS string from the prefix and the four levels."""
    tail = f"{project_code}.{extension}" if extension else project_code
    return f"{prefix}{function_code}/{sub_code}/{tail}"


def fit_tail(project_code, extension=""):
    """Trim a project code so project + extension stay within WBS_TAIL_MAX."""
    return project_code[:max(WBS_TAIL_MAX - len(extension), 1)]


def validate_wbs_parts(function_code, sub_code, project_code, extension="", lang=DEFAULT_LANG):
    """Validate the four WBS levels against the coding standard.

    Raises ValueError with a localized message. Kept free of I/O so the rules
    can be unit-tested in isolation.
    """
    for value in (function_code, sub_code, project_code, extension):
        if value and not re.fullmatch(r"[A-Z0-9-]+", value):
            raise ValueError(t(lang, "error.wbs_charset"))
    if len(function_code or "") != WBS_FUNCTION_LEN:
        raise ValueError(t(lang, "error.wbs_function_len"))
    if len(sub_code or "") != WBS_SUBFUNCTION_LEN:
        raise ValueError(t(lang, "error.wbs_subfunction_len"))
    if not project_code:
        raise ValueError(t(lang, "error.field_required", label=t(lang, "field.project")))
    if len(project_code) + len(extension or "") > WBS_TAIL_MAX:
        raise ValueError(t(lang, "error.wbs_tail_len", max=WBS_TAIL_MAX))


def ref_get_or_create(conn, table, code, name=None, **columns):
    """Return the id of `code` in a reference table, inserting it when absent.

    Used by the demo seed and the legacy migration to fold free text into the
    catalogs. `table` always comes from this module, never from a request.
    """
    code = normalize_ref_code(code) or "N-A"
    row = conn.execute(f"SELECT id FROM {table} WHERE code=?", (code,)).fetchone()
    if row:
        return row["id"]
    values = {"code": code, "name": (name or "").strip() or code,
              "is_active": 1, "created_at": utcnow(), **columns}
    cur = conn.execute(
        f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' * len(values))})",
        tuple(values.values()),
    )
    return cur.lastrowid


def subfunction_get_or_create(conn, function_id, code, name=None):
    """Sub-function ids are unique per function, so this cannot go through
    ref_get_or_create() (which looks a code up on its own)."""
    code = normalize_ref_code(code, WBS_SUBFUNCTION_LEN)
    row = conn.execute("SELECT id FROM sub_functions WHERE function_id=? AND code=?",
                       (function_id, code)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO sub_functions(function_id,code,name,is_active,created_at) VALUES(?,?,?,1,?)",
        (function_id, code, (name or "").strip() or code, utcnow()),
    )
    return cur.lastrowid


def ensure_wbs_element(conn, prefix, function_code, sub_code, project_code, extension="",
                       function_name=None, sub_name=None, project_name=None, name=""):
    """Create the WBS element for these levels (and any missing catalog entry)
    and return its id.

    A clash on the full code is resolved by bumping the extension instead of
    failing, so a migration can never drop a budget line over a duplicate.
    """
    function_id = ref_get_or_create(conn, "functions", function_code, function_name)
    sub_id = subfunction_get_or_create(conn, function_id, sub_code, sub_name)
    project_id = ref_get_or_create(conn, "projects", project_code, project_name)
    fcode = conn.execute("SELECT code FROM functions WHERE id=?", (function_id,)).fetchone()["code"]
    scode = conn.execute("SELECT code FROM sub_functions WHERE id=?", (sub_id,)).fetchone()["code"]
    pcode = conn.execute("SELECT code FROM projects WHERE id=?", (project_id,)).fetchone()["code"]
    base_ext = normalize_ref_code(extension, WBS_TAIL_MAX)
    ext = base_ext
    code = build_wbs(prefix, fcode, scode, fit_tail(pcode, ext), ext)
    attempt = 1
    while conn.execute("SELECT 1 FROM wbs_elements WHERE code=?", (code,)).fetchone():
        attempt += 1
        ext = f"{base_ext}{attempt}" if base_ext else str(attempt)
        code = build_wbs(prefix, fcode, scode, fit_tail(pcode, ext), ext)
    cur = conn.execute(
        """INSERT INTO wbs_elements(code,function_id,sub_function_id,project_id,extension,name,
                                    is_active,created_at)
           VALUES(?,?,?,?,?,?,1,?)""",
        (code, function_id, sub_id, project_id, ext, name, utcnow()),
    )
    return cur.lastrowid


def rebuild_wbs_codes(conn):
    """Recompute every wbs_elements.code from the current prefix and levels.

    Called after the prefix or a level code changes; without it the stored full
    codes would silently drift away from the catalogs they are built from.
    """
    prefix = get_setting(conn, "wbs_prefix", "")
    rows = conn.execute(
        """SELECT w.id, w.extension, f.code fcode, s.code scode, p.code pcode
           FROM wbs_elements w
           JOIN functions f ON f.id=w.function_id
           JOIN sub_functions s ON s.id=w.sub_function_id
           JOIN projects p ON p.id=w.project_id"""
    ).fetchall()
    for row in rows:
        code = build_wbs(prefix, row["fcode"], row["scode"],
                         fit_tail(row["pcode"], row["extension"]), row["extension"])
        conn.execute("UPDATE wbs_elements SET code=? WHERE id=?", (code, row["id"]))


# Declarative description of every catalog: one CRUD implementation serves them
# all, so adding a catalog is a dictionary entry rather than another five
# handlers. `usage` lists the (table, column) references that block a delete.
REFERENCES = {
    "functions": {
        "table": "functions",
        "title": "ref.functions",
        "code_re": r"[A-Z0-9]{2}",
        "code_hint": "hint.function_code",
        "fields": (),
        "usage": (("sub_functions", "function_id"), ("wbs_elements", "function_id")),
    },
    "sub-functions": {
        "table": "sub_functions",
        "title": "ref.sub_functions",
        "code_re": r"[A-Z0-9]{4}",
        "code_hint": "hint.subfunction_code",
        "fields": ({"col": "function_id", "label": "label.function", "kind": "ref",
                    "ref": "functions", "required": True},),
        "usage": (("wbs_elements", "sub_function_id"),),
    },
    "projects": {
        "table": "projects",
        "title": "ref.projects",
        "code_re": r"[A-Z0-9][A-Z0-9-]{0,14}",
        "code_hint": "hint.project_code",
        "fields": (),
        "usage": (("wbs_elements", "project_id"),),
    },
    "cost-centers": {
        "table": "cost_centers",
        "title": "ref.cost_centers",
        "code_re": r"[A-Z0-9][A-Z0-9-]{0,31}",
        "code_hint": "hint.code_generic",
        "fields": (),
        "usage": (("budget_lines", "cost_center_id"),),
    },
    "cost-elements": {
        "table": "cost_elements",
        "title": "ref.cost_elements",
        "code_re": r"[A-Z0-9][A-Z0-9-]{0,31}",
        "code_hint": "hint.code_generic",
        "fields": (),
        "usage": (("budget_lines", "cost_element_id"),),
    },
    "holders": {
        "table": "budget_holders",
        "title": "ref.holders",
        "code_re": r"[A-Z0-9][A-Z0-9-]{0,31}",
        "code_hint": "hint.code_generic",
        "fields": ({"col": "email", "label": "label.email", "kind": "email", "required": False},),
        "usage": (("budget_lines", "holder_id"),),
    },
    "vendors": {
        "table": "vendors",
        "title": "ref.vendors",
        "code_re": r"[A-Z0-9][A-Z0-9-]{0,31}",
        "code_hint": "hint.code_generic",
        "fields": (),
        "usage": (("purchase_orders", "vendor_id"),),
    },
}
# Catalogs whose codes are embedded in a full WBS: editing one rebuilds the
# stored WBS codes.
WBS_LEVEL_TABLES = {"functions", "sub_functions", "projects"}


# --------------------------------------------------------------------------- #
# Schema                                                                       #
# --------------------------------------------------------------------------- #
# Money columns are NUMERIC(18,2) and hold decimal amounts; rates are
# NUMERIC(18,6). No table stores minor units in a separate integer field.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS functions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sub_functions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    function_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(function_id, code),
    FOREIGN KEY(function_id) REFERENCES functions(id)
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_centers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_elements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_holders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vendors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wbs_elements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    function_id INTEGER NOT NULL,
    sub_function_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    extension TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(function_id, sub_function_id, project_id, extension),
    FOREIGN KEY(function_id) REFERENCES functions(id),
    FOREIGN KEY(sub_function_id) REFERENCES sub_functions(id),
    FOREIGN KEY(project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS budget_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    holder_id INTEGER NOT NULL,
    cost_center_id INTEGER,
    cost_element_id INTEGER,
    wbs_element_id INTEGER NOT NULL UNIQUE,
    currency TEXT NOT NULL DEFAULT 'EUR',
    initial_approved NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK(initial_approved >= 0),
    initial_released NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK(initial_released >= 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY(holder_id) REFERENCES budget_holders(id),
    FOREIGN KEY(cost_center_id) REFERENCES cost_centers(id),
    FOREIGN KEY(cost_element_id) REFERENCES cost_elements(id),
    FOREIGN KEY(wbs_element_id) REFERENCES wbs_elements(id)
);

CREATE TABLE IF NOT EXISTS budget_operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_type TEXT NOT NULL,
    source_budget_id INTEGER,
    target_budget_id INTEGER,
    amount NUMERIC(18,2) NOT NULL CHECK(amount > 0),
    approved_delta_source NUMERIC(18,2) NOT NULL DEFAULT 0,
    released_delta_source NUMERIC(18,2) NOT NULL DEFAULT 0,
    approved_delta_target NUMERIC(18,2) NOT NULL DEFAULT 0,
    released_delta_target NUMERIC(18,2) NOT NULL DEFAULT 0,
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
    vendor_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    amount NUMERIC(18,2) NOT NULL CHECK(amount > 0),
    status TEXT NOT NULL CHECK(status IN ('DRAFT','APPROVED','CLOSED','CANCELLED')),
    created_at TEXT NOT NULL,
    FOREIGN KEY(budget_id) REFERENCES budget_lines(id),
    FOREIGN KEY(vendor_id) REFERENCES vendors(id)
);

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    budget_id INTEGER NOT NULL,
    po_id INTEGER,
    expense_date TEXT NOT NULL,
    invoice_no TEXT,
    description TEXT NOT NULL,
    amount NUMERIC(18,2) NOT NULL CHECK(amount > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY(budget_id) REFERENCES budget_lines(id),
    FOREIGN KEY(po_id) REFERENCES purchase_orders(id)
);

CREATE TABLE IF NOT EXISTS budget_monthly_allocations (
    budget_id INTEGER NOT NULL,
    month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
    allocated NUMERIC(18,2) NOT NULL DEFAULT 0 CHECK(allocated >= 0),
    PRIMARY KEY(budget_id, month),
    FOREIGN KEY(budget_id) REFERENCES budget_lines(id)
);

CREATE TABLE IF NOT EXISTS currencies (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    rate NUMERIC(18,6),
    is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0,1)),
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""
SCHEMA_STATEMENTS = [s.strip() for s in SCHEMA_SQL.split(";") if s.strip()]


def create_schema(conn):
    """Create every missing table.

    Statement by statement rather than through executescript(), which commits
    any open transaction before it runs — the migration below needs the DDL to
    stay inside its transaction.
    """
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)


# --------------------------------------------------------------------------- #
# Migration of pre-decimal databases                                           #
# --------------------------------------------------------------------------- #
# Databases created before this change keep money in integer *_cents columns
# (and rates in rate_micro) and carry Budget Holder, Vendor, Cost Center, Cost
# Element and WBS as free text. Everything is copied into the current schema:
# amounts are divided by 100 into Decimals, and the text fields are folded into
# the reference catalogs.
LEGACY_TABLES = ("budget_lines", "purchase_orders", "expenses", "budget_operations",
                 "budget_monthly_allocations", "currencies")
LEGACY_WBS_RE = re.compile(r"([A-Z0-9]{2})/([A-Z0-9]{4})/([A-Z0-9-]{1,15})(?:\.([A-Z0-9-]{1,15}))?")


def table_names(conn):
    return {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def table_columns(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def cents_to_money(cents):
    """Legacy minor units -> the decimal amount they represented."""
    return dec(Decimal(int(cents or 0)) / 100)


def migrate_db():
    """Upgrade a pre-decimal database in place; a no-op on a current one."""
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.create_aggregate("dsum", 1, DecimalSum)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        if "initial_approved_cents" not in table_columns(conn, "budget_lines"):
            return
        print("migrating database to decimal amounts and reference catalogs")
        # Foreign keys stay off for the rename/copy dance, and cannot be
        # toggled inside a transaction — hence the explicit ordering here.
        conn.execute("PRAGMA foreign_keys = OFF")
        present = [name for name in LEGACY_TABLES if name in table_names(conn)]
        conn.execute("BEGIN IMMEDIATE")
        try:
            for name in present:
                conn.execute(f"ALTER TABLE {name} RENAME TO {name}_legacy")
            create_schema(conn)
            migrate_rows(conn, present)
            for name in present:
                conn.execute(f"DROP TABLE {name}_legacy")
            conn.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def migrate_rows(conn, present):
    """Copy every legacy row into the current schema. Ids are preserved so the
    foreign keys between documents keep pointing at the same records."""
    if "currencies" in present:
        for row in conn.execute("SELECT * FROM currencies_legacy"):
            rate = None if row["rate_micro"] is None else dec(
                Decimal(row["rate_micro"]) / 1_000_000, RATE_Q)
            conn.execute(
                "INSERT OR REPLACE INTO currencies(code,name,rate,is_active,updated_at) VALUES(?,?,?,?,?)",
                (row["code"], row["name"], rate, row["is_active"], row["updated_at"]),
            )
    prefix = get_setting(conn, "wbs_prefix", "")
    for row in conn.execute("SELECT * FROM budget_lines_legacy ORDER BY id"):
        holder_name = (row["holder_name"] or "").strip() or "Budget Holder"
        holder_id = ref_get_or_create(conn, "budget_holders", holder_name, holder_name,
                                      email=(row["holder_email"] or "").strip())
        cost_center = (row["cost_center"] or "").strip()
        cost_element = (row["cost_element"] or "").strip()
        cc_id = ref_get_or_create(conn, "cost_centers", cost_center, cost_center) if cost_center else None
        ce_id = ref_get_or_create(conn, "cost_elements", cost_element, cost_element) if cost_element else None
        conn.execute(
            """INSERT INTO budget_lines(id,code,name,fiscal_year,holder_id,cost_center_id,
                                        cost_element_id,wbs_element_id,currency,initial_approved,
                                        initial_released,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["id"], row["code"], row["name"], row["fiscal_year"], holder_id, cc_id, ce_id,
             migrate_wbs(conn, prefix, row["wbs"], row["code"], row["name"]), row["currency"],
             cents_to_money(row["initial_approved_cents"]),
             cents_to_money(row["initial_released_cents"]), row["created_at"]),
        )
    if "purchase_orders" in present:
        for row in conn.execute("SELECT * FROM purchase_orders_legacy ORDER BY id"):
            vendor = (row["vendor"] or "").strip() or "Vendor"
            conn.execute(
                """INSERT INTO purchase_orders(id,number,budget_id,vendor_id,description,amount,
                                               status,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (row["id"], row["number"], row["budget_id"],
                 ref_get_or_create(conn, "vendors", vendor, vendor), row["description"],
                 cents_to_money(row["amount_cents"]), row["status"], row["created_at"]),
            )
    if "expenses" in present:
        for row in conn.execute("SELECT * FROM expenses_legacy ORDER BY id"):
            conn.execute(
                """INSERT INTO expenses(id,budget_id,po_id,expense_date,invoice_no,description,
                                        amount,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (row["id"], row["budget_id"], row["po_id"], row["expense_date"], row["invoice_no"],
                 row["description"], cents_to_money(row["amount_cents"]), row["created_at"]),
            )
    if "budget_operations" in present:
        for row in conn.execute("SELECT * FROM budget_operations_legacy ORDER BY id"):
            conn.execute(
                """INSERT INTO budget_operations(id,operation_type,source_budget_id,target_budget_id,
                                                 amount,approved_delta_source,released_delta_source,
                                                 approved_delta_target,released_delta_target,note,
                                                 created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["id"], row["operation_type"], row["source_budget_id"], row["target_budget_id"],
                 cents_to_money(row["amount_cents"]),
                 cents_to_money(row["approved_delta_source"]),
                 cents_to_money(row["released_delta_source"]),
                 cents_to_money(row["approved_delta_target"]),
                 cents_to_money(row["released_delta_target"]),
                 row["note"], row["created_by"], row["created_at"]),
            )
    if "budget_monthly_allocations" in present:
        for row in conn.execute("SELECT * FROM budget_monthly_allocations_legacy"):
            conn.execute(
                "INSERT INTO budget_monthly_allocations(budget_id,month,allocated) VALUES(?,?,?)",
                (row["budget_id"], row["month"], cents_to_money(row["allocated_cents"])),
            )


def migrate_wbs(conn, prefix, legacy_wbs, budget_code, budget_name):
    """Turn a legacy free-text WBS into a WBS element and return its id.

    Text already written in the standard form is split into its levels; anything
    else (including an empty field) lands under the placeholder function/
    sub-function with the old text as the project code, so nothing is lost and
    the operator can re-file it from the catalogs afterwards.
    """
    match = LEGACY_WBS_RE.fullmatch((legacy_wbs or "").strip().upper())
    if match:
        function_code, sub_code, project_code, extension = (
            match.group(1), match.group(2), match.group(3), match.group(4) or "")
        return ensure_wbs_element(conn, prefix, function_code, sub_code, project_code, extension,
                                  name=budget_name)
    project_code = normalize_ref_code(legacy_wbs or budget_code, WBS_TAIL_MAX) or "LEGACY"
    return ensure_wbs_element(conn, prefix, LEGACY_FUNCTION_CODE, LEGACY_SUBFUNCTION_CODE,
                              project_code, function_name="Legacy", sub_name="Legacy",
                              project_name=(legacy_wbs or budget_code), name=budget_name)


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    migrate_db()
    with db() as conn:
        create_schema(conn)
        # Currency catalog and app settings are core configuration and are
        # seeded regardless of SEED_DEMO. INSERT OR IGNORE keeps it idempotent
        # and never clobbers a rate the operator has already refreshed. RUB is
        # the CBR base: its rate is fixed at 1.0 and it stays active so it can
        # always serve as the default display currency.
        conn.executemany(
            "INSERT OR IGNORE INTO currencies(code,name,rate,is_active) VALUES(?,?,?,?)",
            [
                ("RUB", "Российский рубль", RUB_RATE, 1),
                ("USD", "Доллар США", None, 1),
                ("EUR", "Евро", None, 1),
                ("GBP", "Фунт стерлингов", None, 0),
                ("CNY", "Китайский юань", None, 0),
                ("KZT", "Казахстанский тенге", None, 0),
            ],
        )
        conn.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('base_currency','RUB')")
        conn.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('wbs_prefix','')")
        count = conn.execute("SELECT COUNT(*) FROM budget_lines").fetchone()[0]
    if not (SEED_DEMO and count == 0):
        return
    with db(write=True) as conn:
        seed_demo(conn)


def seed_demo(conn):
    """One budget line with a PO, an expense and a monthly plan so a fresh
    install shows a populated UI. The catalogs are filled first: every document
    field now points at a reference entry."""
    now = utcnow()
    prefix = get_setting(conn, "wbs_prefix", "")
    holder_id = ref_get_or_create(conn, "budget_holders", "BH-DEMO", "Budget Holder",
                                  email="holder@example.com")
    cc_id = ref_get_or_create(conn, "cost_centers", "CC-IT", "IT")
    ce_id = ref_get_or_create(conn, "cost_elements", "IT-SERVICES", "IT Services")
    vendor_id = ref_get_or_create(conn, "vendors", "EXAMPLE", "Example Vendor")
    wbs_id = ensure_wbs_element(conn, prefix, "IT", "OPS1", "INFRA",
                                function_name="Information Technology",
                                sub_name="IT Operations", project_name="Infrastructure",
                                name="IT Operations infrastructure")
    approved = Decimal("100000.00")
    conn.execute(
        """INSERT INTO budget_lines
        (code,name,fiscal_year,holder_id,cost_center_id,cost_element_id,wbs_element_id,currency,
         initial_approved,initial_released,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        ("IT-OPS-2026", "IT Operations", 2026, holder_id, cc_id, ce_id, wbs_id, "EUR",
         approved, approved, now),
    )
    budget_id = conn.execute("SELECT id FROM budget_lines WHERE code='IT-OPS-2026'").fetchone()[0]
    conn.execute(
        """INSERT INTO purchase_orders
        (number,budget_id,vendor_id,description,amount,status,created_at)
        VALUES (?,?,?,?,?,?,?)""",
        ("PO-2026-0001", budget_id, vendor_id, "Infrastructure support, limit PO",
         Decimal("25000.00"), "APPROVED", now),
    )
    po_id = conn.execute("SELECT id FROM purchase_orders WHERE number='PO-2026-0001'").fetchone()[0]
    conn.execute(
        """INSERT INTO expenses
        (budget_id,po_id,expense_date,invoice_no,description,amount,created_at)
        VALUES (?,?,?,?,?,?,?)""",
        (budget_id, po_id, date.today().isoformat(), "INV-DEMO-001", "Monthly support services",
         Decimal("7000.00"), now),
    )
    # Monthly plan: spread released evenly, then shrink the current month below
    # the demo expense (moving the surplus to another month) so the seeded app
    # immediately shows one over-plan month. Total still equals released.
    values = spread_evenly(approved)
    current = date.today().month
    dst = 0 if current == 12 else 11
    floor = Decimal("5000.00")
    if values[current - 1] > floor:
        values[dst] += values[current - 1] - floor
        values[current - 1] = floor
    conn.executemany(
        "INSERT INTO budget_monthly_allocations(budget_id,month,allocated) VALUES(?,?,?)",
        [(budget_id, i + 1, v) for i, v in enumerate(values)],
    )


def parse_money(value, lang=DEFAULT_LANG):
    """Parse a submitted amount into a positive two-decimal Decimal."""
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
        amount = dec(Decimal(text))
    except (InvalidOperation, ValueError):
        raise ValueError(t(lang, "error.bad_amount"))
    if amount <= ZERO:
        raise ValueError(t(lang, "error.amount_positive"))
    return amount


def parse_money_or_zero(value, lang=DEFAULT_LANG):
    """parse_money() for allocation fields, where blank or an explicit zero
    means "no budget this month" rather than an input error."""
    text = (value or "").strip()
    if not text or re.fullmatch(r"0+([.,]0+)?", text.replace(" ", "")):
        return ZERO
    return parse_money(value, lang)


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


def fmt_money(amount, currency="EUR", lang=DEFAULT_LANG):
    s = f"{dec(amount):,.2f}"  # e.g. "1,234.56": comma thousands, dot decimal
    if lang == "ru":
        # Russian formatting: non-breaking-space thousands (U+00A0) and a
        # comma decimal separator, e.g. "1 234,56". English keeps the
        # "1,234.56" grouping produced above.
        s = s.replace(",", " ").replace(".", ",")
    return f"{s} {html.escape(currency)}"


def money_input(amount):
    # Plain decimal string suitable for pre-filling an amount <input> so it
    # round-trips back through parse_money() on the next submit.
    return f"{dec(amount):.2f}"


# Budget lines keep only reference ids, so every read joins the catalogs and
# exposes their codes under the names the pages already use (holder_name,
# cost_center, cost_element, wbs).
BUDGET_SELECT = """SELECT b.*, h.name holder_name, h.email holder_email, h.code holder_code,
                          cc.code cost_center, ce.code cost_element,
                          w.code wbs, w.name wbs_name
                   FROM budget_lines b
                   JOIN budget_holders h ON h.id=b.holder_id
                   JOIN wbs_elements w ON w.id=b.wbs_element_id
                   LEFT JOIN cost_centers cc ON cc.id=b.cost_center_id
                   LEFT JOIN cost_elements ce ON ce.id=b.cost_element_id"""

# Same idea for purchase orders, whose vendor is a catalog entry.
PO_SELECT = """SELECT po.*, v.name vendor, v.code vendor_code
               FROM purchase_orders po
               JOIN vendors v ON v.id=po.vendor_id"""


def budget_metrics(conn, budget_id):
    row = conn.execute(f"{BUDGET_SELECT} WHERE b.id=?", (budget_id,)).fetchone()
    if not row:
        return None
    # dsum() adds Decimals exactly; the four sides are summed separately and
    # combined in Python so no SQL float arithmetic touches the totals.
    op = conn.execute(
        """SELECT
           dsum(CASE WHEN source_budget_id=? THEN approved_delta_source ELSE 0 END) src_approved,
           dsum(CASE WHEN target_budget_id=? THEN approved_delta_target ELSE 0 END) tgt_approved,
           dsum(CASE WHEN source_budget_id=? THEN released_delta_source ELSE 0 END) src_released,
           dsum(CASE WHEN target_budget_id=? THEN released_delta_target ELSE 0 END) tgt_released
           FROM budget_operations""",
        (budget_id, budget_id, budget_id, budget_id),
    ).fetchone()
    actuals = dec(conn.execute(
        "SELECT dsum(amount) FROM expenses WHERE budget_id=?", (budget_id,)
    ).fetchone()[0])
    commitments = dec(conn.execute(
        """SELECT dsum(MAX(po.amount - COALESCE(e.spent,0),0))
           FROM purchase_orders po
           LEFT JOIN (SELECT po_id, dsum(amount) spent FROM expenses WHERE po_id IS NOT NULL GROUP BY po_id) e
             ON e.po_id=po.id
           WHERE po.budget_id=? AND po.status='APPROVED'""",
        (budget_id,),
    ).fetchone()[0])
    approved = dec(row["initial_approved"]) + dec(op["src_approved"]) + dec(op["tgt_approved"])
    released = dec(row["initial_released"]) + dec(op["src_released"]) + dec(op["tgt_released"])
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


def wbs_without_budget(conn):
    """WBS elements that carry no budget line yet.

    A budget has to be formed for every WBS element, so the lists surface the
    ones still missing instead of leaving them to be discovered by accident.
    """
    return conn.execute(
        """SELECT w.id, w.code, w.name FROM wbs_elements w
           LEFT JOIN budget_lines b ON b.wbs_element_id=w.id
           WHERE b.id IS NULL AND w.is_active=1 ORDER BY w.code"""
    ).fetchall()


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
    if m["available"] < ZERO:
        raise ValueError(t(lang, "error.available_negative", code=code))


def spread_evenly(total):
    """Split `total` into 12 monthly amounts differing by at most one cent,
    with the remainder going to the earliest months. Sum is exact."""
    cents = int((dec(total) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    base, extra = divmod(cents, 12)
    return [dec(Decimal(base + (1 if i < extra else 0)) / 100) for i in range(12)]


def monthly_metrics(conn, budget_id):
    """Per-month plan vs actuals for a budget line's fiscal year.

    Expenses are bucketed by expense_date (always YYYY-MM-DD, enforced by
    parse_date). Months are keyed to the line's fiscal_year, so changing the
    year on the line re-buckets actuals automatically. A line with no
    allocation rows has no monthly plan: has_plan is False and no month is
    flagged `over` (legacy annual-only behavior). Monthly control is soft —
    nothing here blocks postings; the hard limit stays in budget_metrics.
    """
    row = conn.execute(f"{BUDGET_SELECT} WHERE b.id=?", (budget_id,)).fetchone()
    if not row:
        return None
    year = str(row["fiscal_year"])
    alloc = {m: dec(v) for m, v in conn.execute(
        "SELECT month, allocated FROM budget_monthly_allocations WHERE budget_id=?",
        (budget_id,)).fetchall()}
    actual = {m: dec(v) for m, v in conn.execute(
        """SELECT CAST(substr(expense_date,6,2) AS INTEGER) m, dsum(amount)
           FROM expenses WHERE budget_id=? AND substr(expense_date,1,4)=? GROUP BY m""",
        (budget_id, year)).fetchall()}
    out_of_year = dec(conn.execute(
        "SELECT dsum(amount) FROM expenses WHERE budget_id=? AND substr(expense_date,1,4)<>?",
        (budget_id, year)).fetchone()[0])
    has_plan = bool(alloc)
    months = []
    for m in range(1, 13):
        allocated = alloc.get(m, ZERO)
        actuals = actual.get(m, ZERO)
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
        "allocated_total": sum(alloc.values(), ZERO),
        "actuals_in_year": sum(actual.values(), ZERO),
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
    sa = sr = ta = tr = ZERO
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
# Rates are decimals relative to RUB (the CBR base): how many roubles one unit
# of the currency is worth, kept to six decimals. RUB itself is exactly 1.
CBR_URL = env_str("CBR_URL", "https://www.cbr.ru/scripts/XML_daily.asp")
CBR_TIMEOUT = env_int("CBR_TIMEOUT", 10, minimum=1)
RUB_RATE = Decimal("1.000000")


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
    """Map currency code -> Decimal rate for every currency that has one.
    RUB is always present at 1.0 so it can serve as the conversion pivot."""
    rates = {r["code"]: dec(r["rate"], RATE_Q) for r in
             conn.execute("SELECT code, rate FROM currencies WHERE rate IS NOT NULL")}
    rates.setdefault("RUB", RUB_RATE)
    return rates


def active_currencies(conn):
    """Active currency rows (code, name, rate, updated_at), code-sorted."""
    return conn.execute(
        "SELECT code, name, rate, updated_at FROM currencies WHERE is_active=1 ORDER BY code"
    ).fetchall()


def convert_money(amount, from_ccy, to_ccy, rates):
    """Convert a decimal amount between two currencies via their rate-to-RUB.
    Returns a two-decimal Decimal in `to_ccy`, or None when either side has no
    known rate. Decimal throughout, so only the final result is rounded."""
    if from_ccy == to_ccy:
        return dec(amount)
    rate_from = rates.get(from_ccy)
    rate_to = rates.get(to_ccy)
    if not rate_from or not rate_to:
        return None
    return dec(dec(amount) * rate_from / rate_to)


def parse_cbr_rates(xml_text):
    """Parse a CBR XML_daily document (already decoded to str) into
    {CharCode: (name, rate)}, the rate being a six-decimal Decimal. VunitRate is
    the value of one unit in RUB (decimal comma). Pure function (no network) so
    it is unit-testable."""
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
        out[code] = (name, dec(per_unit, RATE_Q))
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
    now = utcnow()
    count = 0
    for code, (name, rate) in rates.items():
        if code == "RUB":
            continue
        conn.execute(
            "INSERT INTO currencies(code,name,rate,is_active,updated_at) VALUES(?,?,?,0,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, rate=excluded.rate, "
            "updated_at=excluded.updated_at",
            (code, name, dec(rate, RATE_Q), now),
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
               f'<a href="/wbs">{esc(self.t("nav.wbs"))}</a>'
               f'<a href="/references">{esc(self.t("nav.references"))}</a>'
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

    def money(self, amount, currency="EUR"):
        """fmt_money() bound to the current request language."""
        return fmt_money(amount, currency, getattr(self, "lang", DEFAULT_LANG))

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

    def money_disp(self, amount, native_ccy):
        """Format `amount` (held in native_ccy) for display. When the display
        currency differs, the converted amount leads and the native amount is
        shown muted in parentheses; if no rate exists the native amount is shown
        with a 'no rate' note so a value is never silently dropped."""
        self.ensure_display_context()
        disp = self.display_ccy
        if not native_ccy or native_ccy == disp:
            return fmt_money(amount, native_ccy or disp, self.lang)
        converted = convert_money(amount, native_ccy, disp, self.rates)
        native = fmt_money(amount, native_ccy, self.lang)
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
        if path == "/references":
            return self.references_page()
        m = re.fullmatch(r"/references/([a-z-]+)", path)
        if m:
            return self.reference_list_page(m.group(1))
        m = re.fullmatch(r"/references/([a-z-]+)/(\d+)", path)
        if m:
            return self.reference_edit_page(m.group(1), int(m.group(2)))
        if path == "/wbs":
            return self.wbs_page()
        m = re.fullmatch(r"/wbs/(\d+)", path)
        if m:
            return self.wbs_edit_page(int(m.group(1)))
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
            m = re.fullmatch(r"/references/([a-z-]+)/new", path)
            if m:
                return self.create_reference(m.group(1), data)
            m = re.fullmatch(r"/references/([a-z-]+)/(\d+)/edit", path)
            if m:
                return self.update_reference(m.group(1), int(m.group(2)), data)
            m = re.fullmatch(r"/references/([a-z-]+)/(\d+)/delete", path)
            if m:
                return self.delete_reference(m.group(1), int(m.group(2)), data)
            if path == "/wbs/new":
                return self.create_wbs(data)
            m = re.fullmatch(r"/wbs/(\d+)/edit", path)
            if m:
                return self.update_wbs(int(m.group(1)), data)
            m = re.fullmatch(r"/wbs/(\d+)/delete", path)
            if m:
                return self.delete_wbs(int(m.group(1)), data)
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
        totals = {k: ZERO for k in keys}
        missing = []
        for m in metrics:
            ccy = m["row"]["currency"]
            for k in keys:
                c = convert_money(m[k], ccy, disp, self.rates)
                if c is None:
                    if ccy not in missing:
                        missing.append(ccy)
                else:
                    totals[k] += c
        warn = (f'<div class="flash error">{esc(self.t("misc.dashboard_no_rate", codes=", ".join(sorted(missing))))}</div>'
                if missing else "")
        rows = "".join(
            f"<tr><td>{esc(r['expense_date'])}</td><td><a href='/budgets/{r['budget_id']}'>{esc(r['code'])}</a></td>"
            f"<td>{esc(r['description'])}</td><td>{esc(r['po_number'] or self.t('misc.no_po'))}</td><td>{self.money_disp(r['amount'],r['currency'])}</td></tr>"
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

    # ------------------------------------------------------------------ #
    # Reference pickers shared by the document forms                      #
    # ------------------------------------------------------------------ #
    def reference_rows(self, conn, table, include_id=None):
        """Active entries of a catalog, plus the one a document already points
        at so editing an old record round-trips even after a deactivation."""
        return conn.execute(
            f"SELECT id, code, name, is_active FROM {table} WHERE is_active=1 OR id=? ORDER BY code",
            (include_id or -1,),
        ).fetchall()

    def reference_options(self, rows, selected=None, blank=False):
        """<option> list for a catalog selector, labelled "CODE — Name"."""
        out = ['<option value="">—</option>'] if blank else []
        for r in rows:
            label = r["code"] if r["name"] in (None, "", r["code"]) else f'{r["code"]} — {r["name"]}'
            if not r["is_active"]:
                label = f'{label} ({self.t("misc.inactive")})'
            chosen = " selected" if selected and r["id"] == selected else ""
            out.append(f'<option value="{r["id"]}"{chosen}>{esc(label)}</option>')
        return "".join(out)

    def wbs_options(self, conn, selected=None):
        """<option> list of WBS elements still free to budget (plus the one the
        edited line already uses). A WBS carries at most one budget line."""
        rows = conn.execute(
            """SELECT w.id, w.code, w.name, w.is_active FROM wbs_elements w
               LEFT JOIN budget_lines b ON b.wbs_element_id=w.id
               WHERE (b.id IS NULL AND w.is_active=1) OR w.id=? ORDER BY w.code""",
            (selected or -1,),
        ).fetchall()
        if not rows:
            return ""
        return self.reference_options(rows, selected)

    def wbs_warning(self, conn, confirm_when_clear=False):
        """Banner counting the WBS elements that still lack a budget."""
        pending = wbs_without_budget(conn)
        if not pending:
            if confirm_when_clear:
                return f'<p class="muted small">{esc(self.t("misc.all_wbs_have_budget"))}</p>'
            return ""
        return (f'<div class="flash warn">{esc(self.t("misc.wbs_without_budget", count=len(pending)))} '
                f'<a href="/wbs">{esc(self.t("nav.wbs"))}</a></div>')

    def budgets_page(self):
        # /budgets?wbs=<id> comes from the WBS list, which links here for every
        # element still missing a budget.
        requested_wbs = parse_qs(urlparse(self.path).query).get("wbs", [""])[0]
        preselect = int(requested_wbs) if requested_wbs.isdigit() else None
        with db() as conn:
            metrics = all_budget_metrics(conn)
            holders = self.reference_rows(conn, "budget_holders")
            cost_centers = self.reference_rows(conn, "cost_centers")
            cost_elements = self.reference_rows(conn, "cost_elements")
            wbs_choices = self.wbs_options(conn, preselect)
            warning = self.wbs_warning(conn)
        rows = ""
        for m in metrics:
            r = m["row"]
            usage = 0 if m["released"] <= 0 else min(100, max(0, round((m["actuals"] + m["commitments"]) * 100 / m["released"])))
            rows += f"""<tr><td><a href="/budgets/{r['id']}"><strong>{esc(r['code'])}</strong></a><div class="small muted">{esc(r['name'])}</div></td>
            <td>{r['fiscal_year']}</td><td>{esc(r['holder_name'])}</td><td>{esc(r['cost_center'])}</td><td>{esc(r['wbs'])}</td><td>{esc(r['cost_element'])}</td>
            <td>{self.money_disp(m['released'],r['currency'])}<div class="progress"><span style="width:{usage}%"></span></div></td>
            <td>{self.money_disp(m['actuals'],r['currency'])}</td><td>{self.money_disp(m['commitments'],r['currency'])}</td>
            <td class="{'bad' if m['available'] < 0 else 'good'}"><strong>{self.money_disp(m['available'],r['currency'])}</strong></td>
            <td><a class="button secondary" href="/budgets/{r['id']}/edit">{esc(self.t('btn.edit'))}</a></td></tr>"""
        if wbs_choices:
            wbs_field = (f'<div><label>{esc(self.t("label.wbs_element"))} *</label>'
                         f'<select name="wbs_element_id" required>{wbs_choices}</select></div>')
            submit = f'<button type="submit">{esc(self.t("btn.create_budget"))}</button>'
        else:
            # Nothing to budget against: point at the WBS page instead of
            # offering a form that cannot be submitted.
            wbs_field = (f'<div class="full muted small">{esc(self.t("misc.no_free_wbs"))} '
                         f'<a href="/wbs">{esc(self.t("btn.create_wbs"))}</a></div>')
            submit = ""
        body = f"""<div class="toolbar"><h1>{esc(self.t('nav.budgets'))}</h1>{self.currency_switcher()}</div>{warning}<div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.code'))}</th><th>{esc(self.t('col.year'))}</th><th>{esc(self.t('col.holder'))}</th><th>{esc(self.t('col.cost_center'))}</th><th>{esc(self.t('col.wbs'))}</th><th>{esc(self.t('col.ce'))}</th><th>{esc(self.t('col.released'))}</th><th>{esc(self.t('col.actuals'))}</th><th>{esc(self.t('col.commitments'))}</th><th>{esc(self.t('col.available'))}</th><th>{esc(self.t('col.actions'))}</th></tr></thead><tbody>{rows}</tbody></table></div>
        <br><div class="panel"><h2>{esc(self.t('h2.create_budget'))}</h2><form method="post" action="/budgets/new">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.code'))} *</label><input name="code" required placeholder="IT-OPS-2027"></div><div><label>{esc(self.t('label.name'))} *</label><input name="name" required></div>
        <div><label>{esc(self.t('label.fiscal_year'))} *</label><input type="number" name="fiscal_year" required value="{date.today().year}"></div><div><label>{esc(self.t('label.currency'))} *</label><select name="currency" required>{self.currency_options(self.base_ccy)}</select></div>
        <div><label>{esc(self.t('label.holder'))} *</label><select name="holder_id" required>{self.reference_options(holders)}</select></div>
        {wbs_field}
        <div><label>{esc(self.t('label.cost_center'))}</label><select name="cost_center_id">{self.reference_options(cost_centers, blank=True)}</select></div><div><label>{esc(self.t('label.cost_element'))}</label><select name="cost_element_id">{self.reference_options(cost_elements, blank=True)}</select></div>
        <div><label>{esc(self.t('label.approved'))} *</label><input name="approved" required placeholder="100000.00"></div><div><label>{esc(self.t('label.released'))} *</label><input name="released" required placeholder="100000.00"></div>
        <div class="full">{submit}</div></div></form></div>"""
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
                """SELECT po.*, v.name vendor, dsum(e.amount) spent FROM purchase_orders po
                   JOIN vendors v ON v.id=po.vendor_id
                   LEFT JOIN expenses e ON e.po_id=po.id
                   WHERE po.budget_id=? GROUP BY po.id ORDER BY po.id DESC""", (budget_id,)
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
        target_options = "".join(f'<option value="{b["id"]}">{esc(b["code"])} — {esc(b["name"])}</option>' for b in budgets)
        po_rows = "".join(
            f"<tr><td>{esc(p['number'])}</td><td>{esc(p['vendor'])}</td><td>{esc(p['description'])}</td><td><span class='badge {p['status']}'>{p['status']}</span></td><td>{self.money_disp(p['amount'],r['currency'])}</td><td>{self.money_disp(p['spent'],r['currency'])}</td></tr>" for p in pos
        ) or f"<tr><td colspan='6' class='muted'>{esc(self.t('empty.pos'))}</td></tr>"
        exp_rows = "".join(
            f"<tr><td>{esc(e['expense_date'])}</td><td>{esc(e['invoice_no'])}</td><td>{esc(e['description'])}</td><td>{esc(e['po_number'] or self.t('misc.no_po'))}</td><td>{self.money_disp(e['amount'],r['currency'])}</td></tr>" for e in expenses
        ) or f"<tr><td colspan='5' class='muted'>{esc(self.t('empty.expenses'))}</td></tr>"
        op_rows = "".join(
            f"<tr><td>{esc(o['created_at'][:10])}</td><td>{esc(o['operation_type'])}</td><td>{esc(o['source_code'])}</td><td>{esc(o['target_code'])}</td><td>{self.money_disp(o['amount'],r['currency'])}</td><td>{esc(o['note'])}</td></tr>" for o in ops
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
            f"<input name='alloc_{mo['month']}' value='{money_input(mo['allocated']) if mm['has_plan'] else ''}'></div>"
            for mo in mm["months"])
        monthly_panel = f"""<div class="panel"><h2>{esc(self.t('h2.monthly'))}</h2>{monthly_notes}
        <div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.month'))}</th><th>{esc(self.t('col.allocated'))}</th><th>{esc(self.t('col.actuals'))}</th><th>{esc(self.t('col.remaining'))}</th><th>{esc(self.t('col.status'))}</th></tr></thead><tbody>{month_rows}</tbody></table></div>
        <br><form method="post" action="/budgets/{budget_id}/allocations">{self.csrf_input()}<div class="form-grid">{alloc_inputs}
        <div class="full"><button type="submit">{esc(self.t('btn.save_allocations'))}</button> <button type="submit" name="action" value="distribute" class="secondary">{esc(self.t('btn.distribute_evenly'))}</button></div></div></form>
        <p class="muted small">{esc(self.t('misc.alloc_note'))}</p></div><br>"""
        body = f"""<div class="toolbar"><h1>{esc(r['code'])}: {esc(r['name'])}</h1>{self.currency_switcher()}<a class="button secondary" href="/budgets/{budget_id}/edit">{esc(self.t('btn.edit'))}</a></div>
        <p class="muted">{self.t('misc.budget_meta', holder=esc(r['holder_name']), cost_center=esc(r['cost_center']), wbs=esc(r['wbs']), ce=esc(r['cost_element']))}</p>
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
                """SELECT po.*,b.code,b.currency,v.name vendor,dsum(e.amount) spent FROM purchase_orders po
                   JOIN budget_lines b ON b.id=po.budget_id JOIN vendors v ON v.id=po.vendor_id
                   LEFT JOIN expenses e ON e.po_id=po.id
                   GROUP BY po.id ORDER BY po.id DESC"""
            ).fetchall()
            budgets = all_budget_metrics(conn)
            vendors = self.reference_rows(conn, "vendors")
        rows = ""
        for p in pos:
            remaining = max(dec(p["amount"]) - dec(p["spent"]), ZERO) if p["status"] == "APPROVED" else ZERO
            actions = ""
            if p["status"] == "DRAFT":
                actions = self.status_form(p["id"], "APPROVED", self.t("action.approve")) + " " + self.status_form(p["id"], "CANCELLED", self.t("action.cancel"), "danger")
            elif p["status"] == "APPROVED":
                actions = self.status_form(p["id"], "CLOSED", self.t("action.close"), "secondary") + " " + self.status_form(p["id"], "CANCELLED", self.t("action.cancel"), "danger")
            rows += f"<tr><td><a href='/pos/{p['id']}'>{esc(p['number'])}</a></td><td><a href='/budgets/{p['budget_id']}'>{esc(p['code'])}</a></td><td>{esc(p['vendor'])}</td><td>{esc(p['description'])}</td><td><span class='badge {p['status']}'>{p['status']}</span></td><td>{self.money_disp(p['amount'],p['currency'])}</td><td>{self.money_disp(p['spent'],p['currency'])}</td><td>{self.money_disp(remaining,p['currency'])}</td><td>{actions}</td></tr>"
        budget_options = "".join(f'<option value="{m["row"]["id"]}">{self.t("opt.available", code=esc(m["row"]["code"]), money=self.money(m["available"],m["row"]["currency"]))}</option>' for m in budgets)
        body = f"""<div class="toolbar"><h1>{esc(self.t('h1.pos'))}</h1>{self.currency_switcher()}</div><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.number'))}</th><th>{esc(self.t('col.budget'))}</th><th>{esc(self.t('col.vendor'))}</th><th>{esc(self.t('col.content'))}</th><th>{esc(self.t('col.status'))}</th><th>{esc(self.t('col.amount'))}</th><th>{esc(self.t('col.actuals'))}</th><th>{esc(self.t('col.commitment'))}</th><th>{esc(self.t('col.actions'))}</th></tr></thead><tbody>{rows}</tbody></table></div><br>
        <div class="panel"><h2>{esc(self.t('h2.create_po'))}</h2><form method="post" action="/pos/new">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.number'))} *</label><input name="number" required placeholder="PO-2026-0002"></div><div><label>{esc(self.t('label.budget'))} *</label><select name="budget_id" required>{budget_options}</select></div>
        <div><label>{esc(self.t('label.vendor'))} *</label><select name="vendor_id" required>{self.reference_options(vendors)}</select></div><div><label>{esc(self.t('label.amount_limit'))} *</label><input name="amount" required></div>
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
                """SELECT po.id,po.number,po.budget_id,po.amount,b.currency,dsum(e.amount) spent
                   FROM purchase_orders po JOIN budget_lines b ON b.id=po.budget_id LEFT JOIN expenses e ON e.po_id=po.id
                   WHERE po.status='APPROVED' GROUP BY po.id ORDER BY po.number"""
            ).fetchall()
        rows = "".join(f"<tr><td>{esc(e['expense_date'])}</td><td>{esc(e['code'])}</td><td>{esc(e['po_number'] or self.t('misc.no_po'))}</td><td>{esc(e['invoice_no'])}</td><td>{esc(e['description'])}</td><td>{self.money_disp(e['amount'],e['currency'])}</td><td><a class='button secondary' href='/expenses/{e['id']}'>{esc(self.t('btn.open'))}</a></td></tr>" for e in expenses)
        budget_options = "".join(f'<option value="{m["row"]["id"]}">{self.t("opt.available", code=esc(m["row"]["code"]), money=self.money(m["available"],m["row"]["currency"]))}</option>' for m in budgets)
        po_options = "".join(f'<option value="{p["id"]}">{self.t("opt.remaining", number=esc(p["number"]), money=self.money(max(dec(p["amount"])-dec(p["spent"]),ZERO),p["currency"]))}</option>' for p in pos)
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
                """SELECT o.*,s.code source_code,s.currency source_currency,t.code target_code,t.currency target_currency
                   FROM budget_operations o LEFT JOIN budget_lines s ON s.id=o.source_budget_id
                   LEFT JOIN budget_lines t ON t.id=o.target_budget_id ORDER BY o.id DESC"""
            ).fetchall()
        rows = "".join(f"<tr><td>{esc(o['created_at'])}</td><td>{esc(o['operation_type'])}</td><td>{esc(o['source_code'])}</td><td>{esc(o['target_code'])}</td><td>{self.money_disp(o['amount'],o['source_currency'] or o['target_currency'] or '')}</td><td>{esc(o['created_by'])}</td><td>{esc(o['note'])}</td><td><a class='button secondary' href='/operations/{o['id']}'>{esc(self.t('btn.open'))}</a></td></tr>" for o in ops)
        body = f"""<div class="toolbar"><h1>{esc(self.t('h1.operations'))}</h1>{self.currency_switcher()}</div><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.date'))}</th><th>{esc(self.t('col.operation'))}</th><th>{esc(self.t('col.source'))}</th><th>{esc(self.t('col.target'))}</th><th>{esc(self.t('col.amount'))}</th><th>{esc(self.t('col.executor'))}</th><th>{esc(self.t('col.basis'))}</th><th>{esc(self.t('col.actions'))}</th></tr></thead><tbody>{rows}</tbody></table></div>"""
        self.send_html(self.page(self.t('nav.operations'), body))

    def budget_form_refs(self, data, conn, budget_id=None):
        """Resolve and validate the reference ids submitted by a budget form.

        The WBS element is mandatory and may carry only one budget line, which
        is what makes a WBS unique across budgets.
        """
        holder_id = parse_int(data.get("holder_id"), self.t("field.holder"), self.lang)
        if not conn.execute("SELECT 1 FROM budget_holders WHERE id=?", (holder_id,)).fetchone():
            raise ValueError(self.t("error.ref_not_found"))
        wbs_id = parse_int(data.get("wbs_element_id"), self.t("field.wbs_element"), self.lang)
        if not conn.execute("SELECT 1 FROM wbs_elements WHERE id=?", (wbs_id,)).fetchone():
            raise ValueError(self.t("error.wbs_not_found"))
        taken = conn.execute("SELECT id FROM budget_lines WHERE wbs_element_id=?", (wbs_id,)).fetchone()
        if taken and taken["id"] != budget_id:
            raise ValueError(self.t("error.wbs_taken"))
        optional = {}
        for field, table in (("cost_center_id", "cost_centers"), ("cost_element_id", "cost_elements")):
            raw = (data.get(field) or "").strip()
            if not raw:
                optional[field] = None
                continue
            value = parse_int(raw, self.t(f"label.{field[:-3]}"), self.lang)
            if not conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (value,)).fetchone():
                raise ValueError(self.t("error.ref_not_found"))
            optional[field] = value
        return holder_id, wbs_id, optional["cost_center_id"], optional["cost_element_id"]

    def create_budget(self, data):
        code = require(data, "code", self.t("field.code"), self.lang)
        name = require(data, "name", self.t("field.name"), self.lang)
        fiscal_year = parse_int(data.get("fiscal_year"), self.t("field.fiscal_year"), self.lang)
        approved = parse_money(data.get("approved"), self.lang)
        released = parse_money(data.get("released"), self.lang)
        if released > approved:
            raise ValueError(self.t("error.released_gt_approved_input"))
        currency = data.get("currency", "EUR").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError(self.t("error.currency_format"))
        with db(write=True) as conn:
            if not conn.execute("SELECT 1 FROM currencies WHERE code=? AND is_active=1", (currency,)).fetchone():
                raise ValueError(self.t("error.currency_not_active"))
            holder_id, wbs_id, cc_id, ce_id = self.budget_form_refs(data, conn)
            conn.execute(
                """INSERT INTO budget_lines(code,name,fiscal_year,holder_id,cost_center_id,cost_element_id,
                   wbs_element_id,currency,initial_approved,initial_released,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (code, name, fiscal_year, holder_id, cc_id, ce_id, wbs_id, currency,
                 approved, released, utcnow()),
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
                values = [parse_money_or_zero(data.get(f"alloc_{i}"), self.lang) for i in range(1, 13)]
            conn.execute("DELETE FROM budget_monthly_allocations WHERE budget_id=?", (budget_id,))
            if any(values):
                conn.executemany(
                    "INSERT INTO budget_monthly_allocations(budget_id,month,allocated) VALUES(?,?,?)",
                    [(budget_id, i + 1, v) for i, v in enumerate(values)],
                )
                msg = self.t("flash.allocations_saved")
            else:
                msg = self.t("flash.allocations_cleared")
        self.redirect(f"/budgets/{budget_id}", msg)

    def create_operation(self, budget_id, data):
        op = data.get("operation_type", "").upper()
        amount = parse_money(data.get("amount"), self.lang)
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
            now = utcnow()
            conn.execute(
                """INSERT INTO budget_operations(operation_type,source_budget_id,target_budget_id,amount,approved_delta_source,released_delta_source,approved_delta_target,released_delta_target,note,created_by,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (op,budget_id,target_id,amount,sa,sr,ta,tr,data.get("note","").strip(),data.get("created_by","Budget Holder").strip(),now),
            )
        self.redirect(f"/budgets/{budget_id}", self.t("flash.operation_done"))

    def create_po(self, data):
        number = require(data, "number", self.t("field.po_number"), self.lang)
        vendor_id = parse_int(data.get("vendor_id"), self.t("field.vendor"), self.lang)
        description = require(data, "description", self.t("field.content"), self.lang)
        budget_id = parse_int(data.get("budget_id"), self.t("field.budget"), self.lang)
        amount = parse_money(data.get("amount"), self.lang)
        status = data.get("status", "DRAFT").upper()
        if status not in {"DRAFT","APPROVED"}:
            raise ValueError(self.t("error.bad_po_status"))
        with db(write=True) as conn:
            m = budget_metrics(conn, budget_id)
            if not m:
                raise ValueError(self.t("error.budget_not_found"))
            if not conn.execute("SELECT 1 FROM vendors WHERE id=?", (vendor_id,)).fetchone():
                raise ValueError(self.t("error.ref_not_found"))
            if status == "APPROVED" and amount > m["available"]:
                raise ValueError(self.t("error.insufficient_po_approve"))
            conn.execute(
                "INSERT INTO purchase_orders(number,budget_id,vendor_id,description,amount,status,created_at) VALUES(?,?,?,?,?,?,?)",
                (number, budget_id, vendor_id, description, amount, status, utcnow()),
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
                spent = dec(conn.execute("SELECT dsum(amount) FROM expenses WHERE po_id=?", (po_id,)).fetchone()[0])
                remaining = max(dec(po["amount"]) - spent, ZERO)
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
        amount = parse_money(data.get("amount"), self.lang)
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
                spent = dec(conn.execute("SELECT dsum(amount) FROM expenses WHERE po_id=?", (po_id,)).fetchone()[0])
                if spent + amount > dec(po["amount"]):
                    raise ValueError(self.t("error.expense_exceeds_po"))
            else:
                if amount > m["available"]:
                    raise ValueError(self.t("error.insufficient_no_po"))
            now = utcnow()
            conn.execute(
                "INSERT INTO expenses(budget_id,po_id,expense_date,invoice_no,description,amount,created_at) VALUES(?,?,?,?,?,?,?)",
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
            holders = self.reference_rows(conn, "budget_holders", r["holder_id"])
            cost_centers = self.reference_rows(conn, "cost_centers", r["cost_center_id"])
            cost_elements = self.reference_rows(conn, "cost_elements", r["cost_element_id"])
            wbs_choices = self.wbs_options(conn, r["wbs_element_id"])
        if linked:
            delete_block = f'<p class="muted small">{esc(self.t("misc.budget_delete_blocked", linked=linked))}</p>'
        else:
            delete_block = (f'<form method="post" action="/budgets/{budget_id}/delete">{self.csrf_input()}'
                            f'<button class="danger" type="submit">{esc(self.t("btn.delete_budget"))}</button></form>')
        body = f"""<div class="toolbar"><h1>{self.t('misc.h1_budget_edit', code=esc(r['code']))}</h1><a class="button secondary" href="/budgets/{budget_id}">{esc(self.t('btn.back_to_budget'))}</a></div>
        <div class="panel"><form method="post" action="/budgets/{budget_id}/edit">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.code'))} *</label><input name="code" required value="{esc(r['code'])}"></div><div><label>{esc(self.t('label.name'))} *</label><input name="name" required value="{esc(r['name'])}"></div>
        <div><label>{esc(self.t('label.fiscal_year'))} *</label><input type="number" name="fiscal_year" required value="{r['fiscal_year']}"></div><div><label>{esc(self.t('label.currency'))} *</label><select name="currency" required>{self.currency_options(r['currency'], include=r['currency'])}</select></div>
        <div><label>{esc(self.t('label.holder'))} *</label><select name="holder_id" required>{self.reference_options(holders, r['holder_id'])}</select></div>
        <div><label>{esc(self.t('label.wbs_element'))} *</label><select name="wbs_element_id" required>{wbs_choices}</select></div>
        <div><label>{esc(self.t('label.cost_center'))}</label><select name="cost_center_id">{self.reference_options(cost_centers, r['cost_center_id'], blank=True)}</select></div><div><label>{esc(self.t('label.cost_element'))}</label><select name="cost_element_id">{self.reference_options(cost_elements, r['cost_element_id'], blank=True)}</select></div>
        <div><label>{esc(self.t('label.approved'))} *</label><input name="approved" required value="{money_input(r['initial_approved'])}"></div><div><label>{esc(self.t('label.released'))} *</label><input name="released" required value="{money_input(r['initial_released'])}"></div>
        <div class="full"><button type="submit">{esc(self.t('btn.save_changes'))}</button></div></div></form>
        <p class="muted small">{esc(self.t('misc.edit_budget_note'))}</p></div>
        <br><div class="panel"><h2>{esc(self.t('h2.deletion'))}</h2>{delete_block}</div>"""
        self.send_html(self.page(self.t("title.budget_edit"), body))

    def update_budget(self, budget_id, data):
        code = require(data, "code", self.t("field.code"), self.lang)
        name = require(data, "name", self.t("field.name"), self.lang)
        fiscal_year = parse_int(data.get("fiscal_year"), self.t("field.fiscal_year"), self.lang)
        approved = parse_money(data.get("approved"), self.lang)
        released = parse_money(data.get("released"), self.lang)
        if released > approved:
            raise ValueError(self.t("error.released_gt_approved_input"))
        currency = data.get("currency", "EUR").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError(self.t("error.currency_format"))
        with db(write=True) as conn:
            existing = conn.execute("SELECT currency FROM budget_lines WHERE id=?", (budget_id,)).fetchone()
            if not existing:
                raise ValueError(self.t("error.budget_not_found"))
            if currency != existing["currency"] and not conn.execute(
                    "SELECT 1 FROM currencies WHERE code=? AND is_active=1", (currency,)).fetchone():
                raise ValueError(self.t("error.currency_not_active"))
            holder_id, wbs_id, cc_id, ce_id = self.budget_form_refs(data, conn, budget_id)
            conn.execute(
                """UPDATE budget_lines SET code=?,name=?,fiscal_year=?,holder_id=?,cost_center_id=?,
                   cost_element_id=?,wbs_element_id=?,currency=?,initial_approved=?,initial_released=? WHERE id=?""",
                (code, name, fiscal_year, holder_id, cc_id, ce_id, wbs_id,
                 currency, approved, released, budget_id),
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
            po = conn.execute(f"{PO_SELECT} WHERE po.id=?", (po_id,)).fetchone()
            if not po:
                return self.send_html(self.page(self.t("title.not_found"), f"<h1>{esc(self.t('misc.po_not_found'))}</h1>"), 404)
            budget = conn.execute("SELECT * FROM budget_lines WHERE id=?", (po["budget_id"],)).fetchone()
            spent = dec(conn.execute("SELECT dsum(amount) FROM expenses WHERE po_id=?", (po_id,)).fetchone()[0])
            exp_count = conn.execute("SELECT COUNT(*) FROM expenses WHERE po_id=?", (po_id,)).fetchone()[0]
            budgets = all_budget_metrics(conn)
            vendors = self.reference_rows(conn, "vendors", po["vendor_id"])
        cur = budget["currency"]
        commitment = max(dec(po["amount"]) - spent, ZERO) if po["status"] == "APPROVED" else ZERO
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
            <div><label>{esc(self.t('label.vendor'))} *</label><select name="vendor_id" required>{self.reference_options(vendors, po['vendor_id'])}</select></div><div><label>{esc(self.t('label.amount_limit'))} *</label><input name="amount" required value="{money_input(po['amount'])}"></div>
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
        <div class="card"><div class="label">{esc(self.t('label.amount_limit'))}</div><div class="metric">{self.money_disp(po['amount'], cur)}</div></div>
        <div class="card"><div class="label">{esc(self.t('metric.actuals'))}</div><div class="metric">{self.money_disp(spent, cur)}</div></div>
        <div class="card"><div class="label">{esc(self.t('metric.commitment'))}</div><div class="metric">{self.money_disp(commitment, cur)}</div></div></div>
        <br><div class="toolbar">{actions}</div></div><br>
        {edit_block}<div class="panel"><h2>{esc(self.t('h2.deletion'))}</h2>{delete_block}</div>"""
        self.send_html(self.page(po["number"], body))

    def update_po(self, po_id, data):
        number = require(data, "number", self.t("field.po_number"), self.lang)
        vendor_id = parse_int(data.get("vendor_id"), self.t("field.vendor"), self.lang)
        description = require(data, "description", self.t("field.content"), self.lang)
        budget_id = parse_int(data.get("budget_id"), self.t("field.budget"), self.lang)
        amount = parse_money(data.get("amount"), self.lang)
        with db(write=True) as conn:
            po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (po_id,)).fetchone()
            if not po:
                raise ValueError(self.t("error.po_not_found"))
            if po["status"] not in {"DRAFT", "APPROVED"}:
                raise ValueError(self.t("error.edit_only_draft_approved"))
            if not budget_metrics(conn, budget_id):
                raise ValueError(self.t("error.budget_not_found"))
            if not conn.execute("SELECT 1 FROM vendors WHERE id=?", (vendor_id,)).fetchone():
                raise ValueError(self.t("error.ref_not_found"))
            spent = dec(conn.execute("SELECT dsum(amount) FROM expenses WHERE po_id=?", (po_id,)).fetchone()[0])
            if amount < spent:
                raise ValueError(self.t("error.po_amount_lt_spent"))
            if budget_id != po["budget_id"] and spent > ZERO:
                raise ValueError(self.t("error.cannot_change_budget_with_expenses"))
            conn.execute(
                "UPDATE purchase_orders SET number=?,budget_id=?,vendor_id=?,description=?,amount=? WHERE id=?",
                (number, budget_id, vendor_id, description, amount, po_id),
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
                """SELECT po.id,po.number,po.budget_id,po.amount,po.status,b.currency,
                          dsum(x.amount) spent
                   FROM purchase_orders po JOIN budget_lines b ON b.id=po.budget_id
                   LEFT JOIN expenses x ON x.po_id=po.id
                   WHERE po.status='APPROVED' OR po.id=? GROUP BY po.id ORDER BY po.number""",
                (e["po_id"] or -1,),
            ).fetchall()
        budget_options = "".join(
            f'<option value="{m["row"]["id"]}"{" selected" if m["row"]["id"] == e["budget_id"] else ""}>{self.t("opt.available", code=esc(m["row"]["code"]), money=self.money(m["available"], m["row"]["currency"]))}</option>'
            for m in budgets)
        po_options = f'<option value="">{esc(self.t("misc.no_po"))}</option>' + "".join(
            f'<option value="{p["id"]}"{" selected" if p["id"] == e["po_id"] else ""}>{self.t("opt.remaining", number=esc(p["number"]), money=self.money(max(dec(p["amount"]) - dec(p["spent"]), ZERO), p["currency"]))}{"" if p["status"] == "APPROVED" else " (" + p["status"] + ")"}</option>'
            for p in pos)
        native_ccy = next((mm["row"]["currency"] for mm in budgets if mm["row"]["id"] == e["budget_id"]), self.base_ccy)
        body = f"""<div class="toolbar"><h1>{self.t('misc.h1_expense', id=e['id'])}</h1>{self.currency_switcher()}<a class="button secondary" href="/expenses">{esc(self.t('btn.back_to_expenses'))}</a></div>
        <p class="muted">{esc(self.t('label.amount'))}: {self.money_disp(e['amount'], native_ccy)}</p>
        <div class="panel"><h2>{esc(self.t('h2.edit_expense'))}</h2><form method="post" action="/expenses/{expense_id}/edit">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.budget'))} *</label><select name="budget_id" required>{budget_options}</select></div><div><label>{esc(self.t('label.po'))}</label><select name="po_id">{po_options}</select></div>
        <div><label>{esc(self.t('label.date'))} *</label><input type="date" name="expense_date" value="{esc(e['expense_date'])}" required></div><div><label>{esc(self.t('label.invoice'))}</label><input name="invoice_no" value="{esc(e['invoice_no'])}"></div>
        <div><label>{esc(self.t('label.amount'))} *</label><input name="amount" required value="{money_input(e['amount'])}"></div><div class="full"><label>{esc(self.t('label.description'))} *</label><textarea name="description" required>{esc(e['description'])}</textarea></div>
        <div class="full"><button type="submit">{esc(self.t('btn.save'))}</button></div></div></form></div><br>
        <div class="panel"><h2>{esc(self.t('h2.deletion'))}</h2><form method="post" action="/expenses/{expense_id}/delete">{self.csrf_input()}<button class="danger" type="submit">{esc(self.t('btn.delete_expense'))}</button></form></div>"""
        self.send_html(self.page(self.t('misc.h1_expense', id=e['id']), body))

    def update_expense(self, expense_id, data):
        budget_id = parse_int(data.get("budget_id"), self.t("field.budget"), self.lang)
        po_id = parse_int(data.get("po_id"), self.t("field.po"), self.lang) if data.get("po_id") else None
        expense_date = parse_date(data.get("expense_date"), self.lang)
        description = require(data, "description", self.t("field.description"), self.lang)
        amount = parse_money(data.get("amount"), self.lang)
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
                "UPDATE expenses SET budget_id=?,po_id=?,expense_date=?,invoice_no=?,description=?,amount=? WHERE id=?",
                (budget_id, po_id, expense_date, data.get("invoice_no", "").strip(), description, amount, expense_id),
            )
            if po_id:
                spent = dec(conn.execute("SELECT dsum(amount) FROM expenses WHERE po_id=?", (po_id,)).fetchone()[0])
                po_amount = dec(conn.execute("SELECT amount FROM purchase_orders WHERE id=?", (po_id,)).fetchone()[0])
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
        <div class="panel"><p>{esc(o['operation_type'])} · {self.t('misc.op_source', source=f'<a href="/budgets/{o["source_budget_id"]}">{esc(o["source_code"])}</a>')}{target_line} · {self.money_disp(o['amount'], cur)}</p>
        <p class="muted small">{self.t('misc.op_meta', created_at=esc(o['created_at']), created_by=esc(o['created_by']))}</p></div><br>
        <div class="panel"><h2>{esc(self.t('h2.edit_operation'))}</h2><form method="post" action="/operations/{op_id}/edit">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.op_type'))} *</label><select name="operation_type" required>{self.op_type_options(o["operation_type"])}</select></div><div><label>{esc(self.t('label.amount'))} *</label><input name="amount" required value="{money_input(o['amount'])}"></div>
        <div><label>{esc(self.t('label.target_transfer'))}</label><select name="target_budget_id">{target_options}</select></div><div><label>{esc(self.t('label.executor'))} *</label><input name="created_by" required value="{esc(o['created_by'])}"></div>
        <div class="full"><label>{esc(self.t('label.basis'))} *</label><textarea name="note" required>{esc(o['note'])}</textarea></div>
        <div class="full"><button type="submit">{esc(self.t('btn.save'))}</button></div></div></form>
        <p class="muted small">{esc(self.t('misc.edit_op_note'))}</p></div><br>
        <div class="panel"><h2>{esc(self.t('h2.deletion'))}</h2><form method="post" action="/operations/{op_id}/delete">{self.csrf_input()}<button class="danger" type="submit">{esc(self.t('btn.delete_operation'))}</button></form></div>"""
        self.send_html(self.page(self.t('misc.h1_operation', id=o['id']), body))

    def update_operation(self, op_id, data):
        op = data.get("operation_type", "").upper()
        amount = parse_money(data.get("amount"), self.lang)
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
                """UPDATE budget_operations SET operation_type=?,target_budget_id=?,amount=?,
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
    # Settings: currencies, base currency and CBR rate refresh           #
    # ------------------------------------------------------------------ #
    def settings_page(self):
        with db() as conn:
            currencies = conn.execute(
                "SELECT code,name,rate,is_active,updated_at FROM currencies ORDER BY is_active DESC, code"
            ).fetchall()
            base = get_setting(conn, "base_currency", "RUB")
            rates_updated = get_setting(conn, "rates_updated_at")
            wbs_prefix = get_setting(conn, "wbs_prefix", "")
        active_codes = [c["code"] for c in currencies if c["is_active"]] or [base]
        base_options = "".join(
            f'<option value="{esc(c)}"{" selected" if c == base else ""}>{esc(c)}</option>'
            for c in active_codes)
        cur_rows = ""
        for c in currencies:
            if c["rate"] is None:
                rate = '<span class="muted">—</span>'
            else:
                rate = esc(f'{dec(c["rate"], RATE_Q):.4f}')
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
        <div class="panel"><h2>{esc(self.t('h2.wbs_coding'))}</h2>
        <div class="form-grid"><div><label>{esc(self.t('label.wbs_prefix'))}</label>
        <input name="wbs_prefix" value="{esc(wbs_prefix)}" placeholder="C"></div></div>
        <p class="muted small">{esc(self.t('hint.wbs_prefix'))}</p>
        <p class="muted small">{esc(self.t('misc.wbs_format', example=build_wbs(wbs_prefix, 'IT', 'OPS1', 'INFRA', '01'), max=WBS_TAIL_MAX))}</p></div><br>
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
            # The prefix is part of every full WBS, so a change has to be
            # written through to the stored codes.
            prefix = normalize_ref_code(data.get("wbs_prefix"), 8)
            if prefix != get_setting(conn, "wbs_prefix", ""):
                set_setting(conn, "wbs_prefix", prefix)
                rebuild_wbs_codes(conn)
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

    # ------------------------------------------------------------------ #
    # Reference catalogs: one CRUD implementation for every catalog       #
    # ------------------------------------------------------------------ #
    def reference_spec(self, slug):
        spec = REFERENCES.get(slug)
        if not spec:
            raise ValueError(self.t("error.bad_reference"))
        return spec

    def reference_field_inputs(self, spec, conn, row=None):
        """Render the catalog-specific inputs declared in its spec."""
        out = ""
        for field in spec["fields"]:
            label = esc(self.t(field["label"])) + (" *" if field.get("required") else "")
            value = row[field["col"]] if row else None
            if field["kind"] == "ref":
                options = self.reference_options(
                    self.reference_rows(conn, REFERENCES[field["ref"]]["table"], value),
                    value, blank=not field.get("required"))
                control = f'<select name="{field["col"]}"{" required" if field.get("required") else ""}>{options}</select>'
            else:
                kind = "email" if field["kind"] == "email" else "text"
                control = f'<input type="{kind}" name="{field["col"]}" value="{esc(value or "")}">'
            out += f"<div><label>{label}</label>{control}</div>"
        return out

    def reference_form(self, spec, slug, conn, row=None):
        action = f"/references/{slug}/{row['id']}/edit" if row else f"/references/{slug}/new"
        button = self.t("btn.save") if row else self.t("btn.create_record")
        checked = " checked" if (row is None or row["is_active"]) else ""
        hint = esc(self.t(spec["code_hint"], max=WBS_TAIL_MAX))
        return f"""<form method="post" action="{action}">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.code'))} *</label><input name="code" required value="{esc(row['code'] if row else '')}">
        <div class="muted small">{hint}</div></div>
        <div><label>{esc(self.t('label.name'))} *</label><input name="name" required value="{esc(row['name'] if row else '')}"></div>
        {self.reference_field_inputs(spec, conn, row)}
        <div><label>{esc(self.t('label.active'))}</label><input type="checkbox" name="is_active" value="1"{checked}></div>
        <div class="full"><button type="submit">{esc(button)}</button></div></div></form>"""

    def references_page(self):
        with db() as conn:
            counts = {slug: conn.execute(f"SELECT COUNT(*) FROM {spec['table']}").fetchone()[0]
                      for slug, spec in REFERENCES.items()}
            wbs_count = conn.execute("SELECT COUNT(*) FROM wbs_elements").fetchone()[0]
            ccy_count = conn.execute("SELECT COUNT(*) FROM currencies").fetchone()[0]
            warning = self.wbs_warning(conn)
        cards = "".join(
            f'<div class="card"><div class="label">{esc(self.t(spec["title"]))}</div>'
            f'<div class="metric">{counts[slug]}</div>'
            f'<a href="/references/{slug}">{esc(self.t("btn.open"))}</a></div>'
            for slug, spec in REFERENCES.items())
        # WBS elements and currencies are catalogs too, but each has its own
        # page because of the coding rules and the CBR rates.
        cards += (f'<div class="card"><div class="label">{esc(self.t("ref.wbs"))}</div>'
                  f'<div class="metric">{wbs_count}</div><a href="/wbs">{esc(self.t("btn.open"))}</a></div>'
                  f'<div class="card"><div class="label">{esc(self.t("ref.currencies"))}</div>'
                  f'<div class="metric">{ccy_count}</div><a href="/settings">{esc(self.t("btn.open"))}</a></div>')
        body = (f'<div class="toolbar"><h1>{esc(self.t("h1.references"))}</h1></div>{warning}'
                f'<div class="grid cards">{cards}</div>')
        self.send_html(self.page(self.t("h1.references"), body))

    def reference_list_page(self, slug):
        spec = REFERENCES.get(slug)
        if not spec:
            return self.send_html(self.page(self.t("title.not_found"),
                                            f'<h1>{esc(self.t("error.bad_reference"))}</h1>'), 404)
        with db() as conn:
            rows = conn.execute(f"SELECT * FROM {spec['table']} ORDER BY code").fetchall()
            parents = {}
            for field in spec["fields"]:
                if field["kind"] == "ref":
                    parent_table = REFERENCES[field["ref"]]["table"]
                    parents[field["col"]] = {
                        r["id"]: r["code"] for r in
                        conn.execute(f"SELECT id, code FROM {parent_table}")}
            form = self.reference_form(spec, slug, conn)
        headers = f"<th>{esc(self.t('col.code'))}</th><th>{esc(self.t('col.name'))}</th>"
        for field in spec["fields"]:
            headers += f"<th>{esc(self.t(field['label']))}</th>"
        headers += f"<th>{esc(self.t('col.active'))}</th><th>{esc(self.t('col.actions'))}</th>"
        body_rows = ""
        for r in rows:
            cells = f"<td><strong>{esc(r['code'])}</strong></td><td>{esc(r['name'])}</td>"
            for field in spec["fields"]:
                value = r[field["col"]]
                if field["kind"] == "ref":
                    value = parents[field["col"]].get(value, "")
                cells += f"<td>{esc(value)}</td>"
            cells += (f"<td>{'✓' if r['is_active'] else '—'}</td>"
                      f"<td><a class='button secondary' href='/references/{slug}/{r['id']}'>"
                      f"{esc(self.t('btn.edit'))}</a></td>")
            body_rows += f"<tr>{cells}</tr>"
        colspan = 4 + len(spec["fields"])
        body_rows = body_rows or f"<tr><td colspan='{colspan}' class='muted'>{esc(self.t('empty.records'))}</td></tr>"
        title = self.t(spec["title"])
        body = f"""<div class="toolbar"><h1>{self.t('misc.h1_reference', title=esc(title))}</h1>
        <a class="button secondary" href="/references">{esc(self.t('btn.back_to_references'))}</a></div>
        <div class="table-wrap"><table><thead><tr>{headers}</tr></thead><tbody>{body_rows}</tbody></table></div><br>
        <div class="panel"><h2>{esc(self.t('h2.create_record'))}</h2>{form}</div>"""
        self.send_html(self.page(title, body))

    def reference_edit_page(self, slug, record_id):
        spec = REFERENCES.get(slug)
        if not spec:
            return self.send_html(self.page(self.t("title.not_found"),
                                            f'<h1>{esc(self.t("error.bad_reference"))}</h1>'), 404)
        with db() as conn:
            row = conn.execute(f"SELECT * FROM {spec['table']} WHERE id=?", (record_id,)).fetchone()
            if not row:
                return self.send_html(self.page(self.t("title.not_found"),
                                                f'<h1>{esc(self.t("error.ref_not_found"))}</h1>'), 404)
            used = self.reference_usage(spec, conn, record_id)
            form = self.reference_form(spec, slug, conn, row)
        if used:
            delete_block = f'<p class="muted small">{esc(self.t("misc.ref_delete_blocked", count=used))}</p>'
        else:
            delete_block = (f'<form method="post" action="/references/{slug}/{record_id}/delete">'
                            f'{self.csrf_input()}<button class="danger" type="submit">'
                            f'{esc(self.t("btn.delete_record"))}</button></form>')
        title = self.t(spec["title"])
        body = f"""<div class="toolbar"><h1>{esc(row['code'])}: {esc(row['name'])}</h1>
        <a class="button secondary" href="/references/{slug}">{esc(title)}</a></div>
        <div class="panel"><h2>{esc(self.t('h2.edit_record'))}</h2>{form}</div><br>
        <div class="panel"><h2>{esc(self.t('h2.deletion'))}</h2>{delete_block}</div>"""
        self.send_html(self.page(title, body))

    def reference_usage(self, spec, conn, record_id):
        """How many documents point at this record; non-zero blocks deletion."""
        total = 0
        for table, column in spec["usage"]:
            total += conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (record_id,)).fetchone()[0]
        return total

    def reference_values(self, spec, data, conn, record_id=None):
        """Validate a submitted catalog record into a {column: value} mapping."""
        code = normalize_ref_code(require(data, "code", self.t("field.record_code"), self.lang))
        if not re.fullmatch(spec["code_re"], code):
            raise ValueError(f'{self.t("error.bad_field", label=self.t("field.record_code"))} — '
                             f'{self.t(spec["code_hint"], max=WBS_TAIL_MAX)}')
        # Reported before the UNIQUE constraint fires, so the visitor sees a
        # translated message rather than a raw SQLite error.
        clash = conn.execute(f"SELECT id FROM {spec['table']} WHERE code=?", (code,)).fetchone()
        if clash and clash["id"] != record_id:
            raise ValueError(self.t("error.code_exists"))
        values = {
            "code": code,
            "name": require(data, "name", self.t("field.record_name"), self.lang),
            "is_active": 1 if data.get("is_active") == "1" else 0,
        }
        for field in spec["fields"]:
            raw = (data.get(field["col"]) or "").strip()
            if field["kind"] == "ref":
                if not raw:
                    if field.get("required"):
                        raise ValueError(self.t("error.field_required", label=self.t(field["label"])))
                    values[field["col"]] = None
                    continue
                parent = REFERENCES[field["ref"]]["table"]
                parent_id = parse_int(raw, self.t(field["label"]), self.lang)
                if not conn.execute(f"SELECT 1 FROM {parent} WHERE id=?", (parent_id,)).fetchone():
                    raise ValueError(self.t("error.ref_not_found"))
                values[field["col"]] = parent_id
            else:
                values[field["col"]] = raw
        return values

    def create_reference(self, slug, data):
        spec = self.reference_spec(slug)
        with db(write=True) as conn:
            values = self.reference_values(spec, data, conn)
            values["created_at"] = utcnow()
            columns = ",".join(values)
            conn.execute(f"INSERT INTO {spec['table']}({columns}) "
                         f"VALUES({','.join('?' * len(values))})", tuple(values.values()))
        self.redirect(f"/references/{slug}", self.t("flash.record_created"))

    def update_reference(self, slug, record_id, data):
        spec = self.reference_spec(slug)
        with db(write=True) as conn:
            if not conn.execute(f"SELECT 1 FROM {spec['table']} WHERE id=?", (record_id,)).fetchone():
                raise ValueError(self.t("error.ref_not_found"))
            values = self.reference_values(spec, data, conn, record_id)
            assignments = ",".join(f"{column}=?" for column in values)
            conn.execute(f"UPDATE {spec['table']} SET {assignments} WHERE id=?",
                         (*values.values(), record_id))
            if spec["table"] in WBS_LEVEL_TABLES:
                rebuild_wbs_codes(conn)
        self.redirect(f"/references/{slug}/{record_id}", self.t("flash.record_updated"))

    def delete_reference(self, slug, record_id, data):
        spec = self.reference_spec(slug)
        with db(write=True) as conn:
            if not conn.execute(f"SELECT 1 FROM {spec['table']} WHERE id=?", (record_id,)).fetchone():
                raise ValueError(self.t("error.ref_not_found"))
            if self.reference_usage(spec, conn, record_id):
                raise ValueError(self.t("error.ref_in_use"))
            conn.execute(f"DELETE FROM {spec['table']} WHERE id=?", (record_id,))
        self.redirect(f"/references/{slug}", self.t("flash.record_deleted"))

    # ------------------------------------------------------------------ #
    # WBS elements                                                        #
    # ------------------------------------------------------------------ #
    def subfunction_options(self, conn, selected=None):
        """Sub-functions labelled with their function, since the code alone is
        only unique within a function."""
        rows = conn.execute(
            """SELECT s.id, s.code, s.name, s.is_active, f.code fcode FROM sub_functions s
               JOIN functions f ON f.id=s.function_id
               WHERE s.is_active=1 OR s.id=? ORDER BY f.code, s.code""",
            (selected or -1,)).fetchall()
        out = []
        for r in rows:
            label = f'{r["fcode"]}/{r["code"]} — {r["name"]}'
            chosen = " selected" if selected and r["id"] == selected else ""
            out.append(f'<option value="{r["id"]}"{chosen}>{esc(label)}</option>')
        return "".join(out)

    def wbs_form(self, conn, row=None):
        action = f"/wbs/{row['id']}/edit" if row else "/wbs/new"
        button = self.t("btn.save") if row else self.t("btn.create_wbs")
        functions = self.reference_rows(conn, "functions", row["function_id"] if row else None)
        projects = self.reference_rows(conn, "projects", row["project_id"] if row else None)
        return f"""<form method="post" action="{action}">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.function'))} *</label><select name="function_id" required>
        {self.reference_options(functions, row['function_id'] if row else None)}</select>
        <div class="muted small">{esc(self.t('hint.function_code'))}</div></div>
        <div><label>{esc(self.t('label.sub_function'))} *</label><select name="sub_function_id" required>
        {self.subfunction_options(conn, row['sub_function_id'] if row else None)}</select>
        <div class="muted small">{esc(self.t('hint.subfunction_code'))}</div></div>
        <div><label>{esc(self.t('label.project'))} *</label><select name="project_id" required>
        {self.reference_options(projects, row['project_id'] if row else None)}</select>
        <div class="muted small">{esc(self.t('hint.project_code', max=WBS_TAIL_MAX))}</div></div>
        <div><label>{esc(self.t('label.extension'))}</label>
        <input name="extension" value="{esc(row['extension'] if row else '')}">
        <div class="muted small">{esc(self.t('hint.extension', max=WBS_TAIL_MAX))}</div></div>
        <div class="full"><label>{esc(self.t('label.name'))}</label>
        <input name="name" value="{esc(row['name'] if row else '')}"></div>
        <div class="full"><button type="submit">{esc(button)}</button></div></div></form>"""

    def wbs_page(self):
        with db() as conn:
            rows = conn.execute(
                """SELECT w.*, f.code fcode, s.code scode, p.code pcode,
                          b.id budget_id, b.code budget_code
                   FROM wbs_elements w
                   JOIN functions f ON f.id=w.function_id
                   JOIN sub_functions s ON s.id=w.sub_function_id
                   JOIN projects p ON p.id=w.project_id
                   LEFT JOIN budget_lines b ON b.wbs_element_id=w.id
                   ORDER BY w.code"""
            ).fetchall()
            prefix = get_setting(conn, "wbs_prefix", "")
            warning = self.wbs_warning(conn, confirm_when_clear=bool(rows))
            form = self.wbs_form(conn)
        body_rows = ""
        for r in rows:
            if r["budget_id"]:
                budget = f'<a href="/budgets/{r["budget_id"]}">{esc(r["budget_code"])}</a>'
            else:
                # A budget has to exist for every WBS element, so the missing
                # ones link straight into the budget form with this WBS chosen.
                budget = (f'<span class="badge OVER">{esc(self.t("misc.wbs_no_budget"))}</span> '
                          f'<a href="/budgets?wbs={r["id"]}">{esc(self.t("btn.create_budget"))}</a>')
            body_rows += (f"<tr><td><strong>{esc(r['code'])}</strong></td><td>{esc(r['name'])}</td>"
                          f"<td>{esc(r['fcode'])}</td><td>{esc(r['scode'])}</td>"
                          f"<td>{esc(r['pcode'])}</td><td>{esc(r['extension'])}</td>"
                          f"<td>{budget}</td>"
                          f"<td><a class='button secondary' href='/wbs/{r['id']}'>"
                          f"{esc(self.t('btn.edit'))}</a></td></tr>")
        body_rows = body_rows or f"<tr><td colspan='8' class='muted'>{esc(self.t('empty.wbs'))}</td></tr>"
        note = esc(self.t("misc.wbs_format",
                          example=build_wbs(prefix, "IT", "OPS1", "INFRA", "01"), max=WBS_TAIL_MAX))
        body = f"""<div class="toolbar"><h1>{esc(self.t('h1.wbs'))}</h1>
        <a class="button secondary" href="/references">{esc(self.t('btn.back_to_references'))}</a></div>
        {warning}<p class="muted small">{note}</p>
        <div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.wbs'))}</th><th>{esc(self.t('col.name'))}</th>
        <th>{esc(self.t('col.function'))}</th><th>{esc(self.t('col.sub_function'))}</th>
        <th>{esc(self.t('col.project'))}</th><th>{esc(self.t('col.extension'))}</th>
        <th>{esc(self.t('col.budget'))}</th><th>{esc(self.t('col.actions'))}</th></tr></thead>
        <tbody>{body_rows}</tbody></table></div><br>
        <div class="panel"><h2>{esc(self.t('h2.create_wbs'))}</h2>{form}</div>"""
        self.send_html(self.page(self.t("h1.wbs"), body))

    def wbs_edit_page(self, wbs_id):
        with db() as conn:
            row = conn.execute("SELECT * FROM wbs_elements WHERE id=?", (wbs_id,)).fetchone()
            if not row:
                return self.send_html(self.page(self.t("title.not_found"),
                                                f'<h1>{esc(self.t("error.wbs_not_found"))}</h1>'), 404)
            budget = conn.execute("SELECT id, code FROM budget_lines WHERE wbs_element_id=?",
                                  (wbs_id,)).fetchone()
            form = self.wbs_form(conn, row)
        if budget:
            delete_block = f'<p class="muted small">{esc(self.t("misc.wbs_delete_blocked"))}</p>'
            budget_line = (f'<p class="muted">{esc(self.t("col.budget"))}: '
                           f'<a href="/budgets/{budget["id"]}">{esc(budget["code"])}</a></p>')
        else:
            delete_block = (f'<form method="post" action="/wbs/{wbs_id}/delete">{self.csrf_input()}'
                            f'<button class="danger" type="submit">{esc(self.t("btn.delete_wbs"))}</button></form>')
            budget_line = (f'<p class="muted">{esc(self.t("misc.wbs_no_budget"))} '
                           f'<a href="/budgets?wbs={wbs_id}">{esc(self.t("btn.create_budget"))}</a></p>')
        body = f"""<div class="toolbar"><h1>{self.t('misc.h1_wbs', code=esc(row['code']))}</h1>
        <a class="button secondary" href="/wbs">{esc(self.t('btn.back_to_wbs'))}</a></div>
        {budget_line}
        <div class="panel"><h2>{esc(self.t('h2.edit_wbs'))}</h2>{form}</div><br>
        <div class="panel"><h2>{esc(self.t('h2.deletion'))}</h2>{delete_block}</div>"""
        self.send_html(self.page(row["code"], body))

    def wbs_values(self, data, conn, wbs_id=None):
        """Validate a submitted WBS element and return the row to write.

        Returns (code, function_id, sub_function_id, project_id, extension, name).
        """
        function_id = parse_int(data.get("function_id"), self.t("field.function"), self.lang)
        sub_id = parse_int(data.get("sub_function_id"), self.t("field.sub_function"), self.lang)
        project_id = parse_int(data.get("project_id"), self.t("field.project"), self.lang)
        function = conn.execute("SELECT code FROM functions WHERE id=?", (function_id,)).fetchone()
        sub = conn.execute("SELECT code, function_id FROM sub_functions WHERE id=?", (sub_id,)).fetchone()
        project = conn.execute("SELECT code FROM projects WHERE id=?", (project_id,)).fetchone()
        if not function or not sub or not project:
            raise ValueError(self.t("error.ref_not_found"))
        if sub["function_id"] != function_id:
            raise ValueError(self.t("error.subfunction_not_in_function"))
        extension = normalize_ref_code(data.get("extension"), WBS_TAIL_MAX)
        validate_wbs_parts(function["code"], sub["code"], project["code"], extension, self.lang)
        code = build_wbs(get_setting(conn, "wbs_prefix", ""),
                         function["code"], sub["code"], project["code"], extension)
        clash = conn.execute("SELECT id FROM wbs_elements WHERE code=?", (code,)).fetchone()
        if clash and clash["id"] != wbs_id:
            raise ValueError(self.t("error.wbs_exists"))
        return code, function_id, sub_id, project_id, extension, (data.get("name") or "").strip()

    def create_wbs(self, data):
        with db(write=True) as conn:
            code, function_id, sub_id, project_id, extension, name = self.wbs_values(data, conn)
            conn.execute(
                """INSERT INTO wbs_elements(code,function_id,sub_function_id,project_id,extension,
                                            name,is_active,created_at)
                   VALUES(?,?,?,?,?,?,1,?)""",
                (code, function_id, sub_id, project_id, extension, name, utcnow()),
            )
        self.redirect("/wbs", self.t("flash.wbs_created"))

    def update_wbs(self, wbs_id, data):
        with db(write=True) as conn:
            if not conn.execute("SELECT 1 FROM wbs_elements WHERE id=?", (wbs_id,)).fetchone():
                raise ValueError(self.t("error.wbs_not_found"))
            code, function_id, sub_id, project_id, extension, name = self.wbs_values(data, conn, wbs_id)
            conn.execute(
                """UPDATE wbs_elements SET code=?,function_id=?,sub_function_id=?,project_id=?,
                   extension=?,name=? WHERE id=?""",
                (code, function_id, sub_id, project_id, extension, name, wbs_id),
            )
        self.redirect(f"/wbs/{wbs_id}", self.t("flash.wbs_updated"))

    def delete_wbs(self, wbs_id, data):
        with db(write=True) as conn:
            if not conn.execute("SELECT 1 FROM wbs_elements WHERE id=?", (wbs_id,)).fetchone():
                raise ValueError(self.t("error.wbs_not_found"))
            if conn.execute("SELECT 1 FROM budget_lines WHERE wbs_element_id=?", (wbs_id,)).fetchone():
                raise ValueError(self.t("error.wbs_delete_has_budget"))
            conn.execute("DELETE FROM wbs_elements WHERE id=?", (wbs_id,))
        self.redirect("/wbs", self.t("flash.wbs_deleted"))

    def api_summary(self):
        with db() as conn:
            metrics = all_budget_metrics(conn)
        payload = []
        for m in metrics:
            r=m["row"]
            # Amounts go out as two-decimal strings: JSON numbers are binary
            # floats and would reintroduce exactly the rounding this schema
            # avoids.
            payload.append({
                "id": r["id"], "code": r["code"], "name": r["name"], "fiscal_year": r["fiscal_year"],
                "currency": r["currency"], "holder": r["holder_name"], "wbs": r["wbs"],
                "cost_center": r["cost_center"], "cost_element": r["cost_element"],
                "approved": str(m["approved"]), "released": str(m["released"]),
                "actuals": str(m["actuals"]), "commitments": str(m["commitments"]),
                "available": str(m["available"]),
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
