#!/usr/bin/env python3
import base64
import contextlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

APP_NAME = "Budget Control"
DB_PATH = os.getenv("DB_PATH", "/data/budget.db")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
APP_USER = os.getenv("APP_USER", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SEED_DEMO = os.getenv("SEED_DEMO", "1") == "1"

# --------------------------------------------------------------------------- #
# Internationalization (i18n)                                                  #
# --------------------------------------------------------------------------- #
DEFAULT_LANG = os.getenv("DEFAULT_LANG", "en")   # UI language when nothing else is set
LANGUAGES = ("en", "ru")                          # supported UI languages (switcher order)
LANG_COOKIE = "lang"                              # cookie remembering the visitor's choice

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
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
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
            """
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
        conn.execute(
            """INSERT INTO budget_lines
            (code,name,fiscal_year,holder_name,holder_email,cost_center,wbs,cost_element,currency,
             initial_approved_cents,initial_released_cents,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("IT-OPS-2026", "IT Operations", 2026, "Budget Holder", "holder@example.com",
             "CC-IT", "WBS-IT-OPS", "IT Services", "EUR", 10000000, 10000000, now),
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
CBR_URL = os.getenv("CBR_URL", "https://www.cbr.ru/scripts/XML_daily.asp")
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


def fetch_cbr_rates(url=None, timeout=10):
    """Fetch and parse today's CBR rates. Network is isolated from parsing so
    tests can stub this out. Raises ValueError on any network/parse failure."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url or CBR_URL, headers={"User-Agent": "BudgetControl/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
.flash{padding:12px 14px;border-radius:8px;margin-bottom:16px;background:#dbeafe;color:#1e3a8a}.flash.error{background:#fee2e2;color:#991b1b}.muted{color:var(--muted)}.small{font-size:12px}.split{display:grid;grid-template-columns:2fr 1fr;gap:16px}@media(max-width:850px){.split{grid-template-columns:1fr}.nav{gap:10px}.top{align-items:flex-start;padding:14px 0;flex-direction:column}}
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

    def redirect(self, path, message=None, error=False):
        if message:
            sep = "&" if "?" in path else "?"
            path += sep + urlencode({"msg": message, "kind": "error" if error else "ok"})
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
        if is_new:
            self.send_header("Set-Cookie", f"csrf_token={token}; Path=/; SameSite=Strict; HttpOnly")
        pending_lang = getattr(self, "_set_lang_cookie", None)
        if pending_lang:
            self.send_header("Set-Cookie", f"{LANG_COOKIE}={pending_lang}; Path=/; SameSite=Lax")
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
            cls = "flash error" if q.get("kind", [""])[0] == "error" else "flash"
            flash = f'<div class="{cls}">{esc(q["msg"][0])}</div>'
        nav = (f'<a href="/">{esc(self.t("nav.overview"))}</a>'
               f'<a href="/budgets">{esc(self.t("nav.budgets"))}</a>'
               f'<a href="/pos">{esc(self.t("nav.pos"))}</a>'
               f'<a href="/expenses">{esc(self.t("nav.expenses"))}</a>'
               f'<a href="/operations">{esc(self.t("nav.operations"))}</a>'
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
        if not self._require_auth():
            return
        self.lang = self.resolve_lang()
        self._disp_loaded = False
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/static/style.css":
            body = CSS.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if path == "/healthz":
            self.send_json({"status": "ok"}); return
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
        body = f"""<div class="toolbar"><h1>{esc(self.t('nav.budgets'))}</h1>{self.currency_switcher()}</div><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.code'))}</th><th>{esc(self.t('col.year'))}</th><th>{esc(self.t('col.holder'))}</th><th>{esc(self.t('col.cost_center'))}</th><th>{esc(self.t('col.wbs'))}</th><th>{esc(self.t('col.ce'))}</th><th>{esc(self.t('col.released'))}</th><th>{esc(self.t('col.actuals'))}</th><th>{esc(self.t('col.commitments'))}</th><th>{esc(self.t('col.available'))}</th><th>{esc(self.t('col.actions'))}</th></tr></thead><tbody>{rows}</tbody></table></div>
        <br><div class="panel"><h2>{esc(self.t('h2.create_budget'))}</h2><form method="post" action="/budgets/new">{self.csrf_input()}<div class="form-grid">
        <div><label>{esc(self.t('label.code'))} *</label><input name="code" required placeholder="IT-OPS-2027"></div><div><label>{esc(self.t('label.name'))} *</label><input name="name" required></div>
        <div><label>{esc(self.t('label.fiscal_year'))} *</label><input type="number" name="fiscal_year" required value="{date.today().year}"></div><div><label>{esc(self.t('label.currency'))} *</label><select name="currency" required>{self.currency_options(self.base_ccy)}</select></div>
        <div><label>{esc(self.t('label.holder'))} *</label><input name="holder_name" required></div><div><label>{esc(self.t('label.email'))}</label><input type="email" name="holder_email"></div>
        <div><label>{esc(self.t('label.cost_center'))}</label><input name="cost_center"></div><div><label>{esc(self.t('label.wbs'))}</label><input name="wbs"></div><div><label>{esc(self.t('label.cost_element'))}</label><input name="cost_element"></div>
        <div><label>{esc(self.t('label.approved'))} *</label><input name="approved" required placeholder="100000.00"></div><div><label>{esc(self.t('label.released'))} *</label><input name="released" required placeholder="100000.00"></div>
        <div class="full"><button type="submit">{esc(self.t('btn.create_budget'))}</button></div></div></form></div>"""
        self.send_html(self.page(self.t('nav.budgets'), body))

    def budget_detail(self, budget_id):
        with db() as conn:
            m = budget_metrics(conn, budget_id)
            if not m:
                return self.send_html(self.page(self.t("title.not_found"), f"<h1>{esc(self.t('misc.budget_not_found'))}</h1>"), 404)
            r = m["row"]
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
        body = f"""<div class="toolbar"><h1>{esc(r['code'])}: {esc(r['name'])}</h1>{self.currency_switcher()}<a class="button secondary" href="/budgets/{budget_id}/edit">{esc(self.t('btn.edit'))}</a></div>
        <p class="muted">{self.t('misc.budget_meta', holder=esc(r['holder_name']), cost_center=esc(r['cost_center']), wbs=esc(r['wbs']), ce=esc(r['cost_element']))}</p>
        <div class="grid cards"><div class="card"><div class="label">{esc(self.t('metric.approved'))}</div><div class="metric">{self.money_disp(m['approved'],r['currency'])}</div></div>
        <div class="card"><div class="label">{esc(self.t('metric.released'))}</div><div class="metric">{self.money_disp(m['released'],r['currency'])}</div></div>
        <div class="card"><div class="label">{esc(self.t('metric.actuals'))}</div><div class="metric">{self.money_disp(m['actuals'],r['currency'])}</div></div>
        <div class="card"><div class="label">{esc(self.t('metric.commitments'))}</div><div class="metric">{self.money_disp(m['commitments'],r['currency'])}</div></div>
        <div class="card"><div class="label">{esc(self.t('metric.available'))}</div><div class="metric {'bad' if m['available']<0 else 'good'}">{self.money_disp(m['available'],r['currency'])}</div></div></div><br>
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
        with db() as conn:
            expenses = conn.execute(
                """SELECT e.*,b.code,b.currency,po.number po_number FROM expenses e JOIN budget_lines b ON b.id=e.budget_id
                   LEFT JOIN purchase_orders po ON po.id=e.po_id ORDER BY e.expense_date DESC,e.id DESC"""
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
        body = f"""<div class="toolbar"><h1>{esc(self.t('h1.expenses'))}</h1>{self.currency_switcher()}</div><div class="table-wrap"><table><thead><tr><th>{esc(self.t('col.date'))}</th><th>{esc(self.t('col.budget'))}</th><th>{esc(self.t('col.po'))}</th><th>{esc(self.t('col.invoice'))}</th><th>{esc(self.t('col.description'))}</th><th>{esc(self.t('col.amount'))}</th><th>{esc(self.t('col.actions'))}</th></tr></thead><tbody>{rows}</tbody></table></div><br>
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
        rows = "".join(f"<tr><td>{esc(o['created_at'])}</td><td>{esc(o['operation_type'])}</td><td>{esc(o['source_code'])}</td><td>{esc(o['target_code'])}</td><td>{self.money_disp(o['amount_cents'],o['source_currency'] or o['target_currency'] or '')}</td><td>{esc(o['created_by'])}</td><td>{esc(o['note'])}</td><td><a class='button secondary' href='/operations/{o['id']}'>{esc(self.t('btn.open'))}</a></td></tr>" for o in ops)
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
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        with db(write=True) as conn:
            if not conn.execute("SELECT 1 FROM currencies WHERE code=? AND is_active=1", (currency,)).fetchone():
                raise ValueError(self.t("error.currency_not_active"))
            conn.execute(
                """INSERT INTO budget_lines(code,name,fiscal_year,holder_name,holder_email,cost_center,wbs,cost_element,currency,initial_approved_cents,initial_released_cents,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (code, name, fiscal_year, holder_name, data.get("holder_email","").strip(), data.get("cost_center","").strip(), data.get("wbs","").strip(), data.get("cost_element","").strip(), currency, approved, released, now),
            )
        self.redirect("/budgets", self.t("flash.budget_created"))

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
        <div><label>{esc(self.t('label.cost_center'))}</label><input name="cost_center" value="{esc(r['cost_center'])}"></div><div><label>{esc(self.t('label.wbs'))}</label><input name="wbs" value="{esc(r['wbs'])}"></div><div><label>{esc(self.t('label.cost_element'))}</label><input name="cost_element" value="{esc(r['cost_element'])}"></div>
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
        with db(write=True) as conn:
            existing = conn.execute("SELECT currency FROM budget_lines WHERE id=?", (budget_id,)).fetchone()
            if not existing:
                raise ValueError(self.t("error.budget_not_found"))
            if currency != existing["currency"] and not conn.execute(
                    "SELECT 1 FROM currencies WHERE code=? AND is_active=1", (currency,)).fetchone():
                raise ValueError(self.t("error.currency_not_active"))
            conn.execute(
                """UPDATE budget_lines SET code=?,name=?,fiscal_year=?,holder_name=?,holder_email=?,
                   cost_center=?,wbs=?,cost_element=?,currency=?,initial_approved_cents=?,initial_released_cents=? WHERE id=?""",
                (code, name, fiscal_year, holder_name, data.get("holder_email", "").strip(),
                 data.get("cost_center", "").strip(), data.get("wbs", "").strip(), data.get("cost_element", "").strip(),
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
        payload = []
        for m in metrics:
            r=m["row"]
            payload.append({
                "id":r["id"],"code":r["code"],"name":r["name"],"fiscal_year":r["fiscal_year"],
                "currency":r["currency"],"holder":r["holder_name"],"approved_cents":m["approved"],
                "released_cents":m["released"],"actuals_cents":m["actuals"],"commitments_cents":m["commitments"],"available_cents":m["available"]
            })
        self.send_json({"budgets": payload})


def main():
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"{APP_NAME} listening on http://{HOST}:{PORT}; DB={DB_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
