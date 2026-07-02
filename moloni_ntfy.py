#!/usr/bin/env python3
"""
Moloni -> ntfy notifier.

Polls the Moloni (classic, API v1) documents endpoint and sends a push
notification through ntfy whenever a new in-store sale shows up.

Commands:
    run           Run forever, polling every POLL_INTERVAL_SECONDS (cloud/daemon).
    once          Do a single poll and exit (use from cron / serverless).
    check         Validate config + Moloni auth + company; print a summary.
    list-types    Print document types, document sets and the latest documents,
                  to help you pick what counts as an "in-store sale".
    test-notify   Send a test notification through ntfy.

Configuration comes from environment variables (see .env.example).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import date, datetime, timedelta

import requests

LOG = logging.getLogger("moloni-ntfy")

MOLONI_BASE_URL = "https://api.moloni.pt/v1"
GRANT_URL = f"{MOLONI_BASE_URL}/grant/"

# ntfy priority names -> JSON priority numbers
NTFY_PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}

# Moloni OAuth error codes that mean "the token is no good, re-authenticate".
TOKEN_ERRORS = {"invalid_token", "access_denied", "invalid_grant", "unauthorized"}


class AuthError(Exception):
    """Raised when Moloni rejects our token and we must re-authenticate."""


class MoloniError(Exception):
    """Raised for non-auth Moloni API errors."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _load_env_file(path: str = ".env") -> None:
    """Load a .env file. Uses python-dotenv when available, else a tiny parser."""
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(path)
        return
    except Exception:
        pass
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _int_or_none(value):
    if value is None:
        return None
    value = str(value).strip()
    return int(value) if value else None


class Config:
    def __init__(self) -> None:
        # Moloni credentials
        self.client_id = os.getenv("MOLONI_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("MOLONI_CLIENT_SECRET", "").strip()
        self.username = os.getenv("MOLONI_USERNAME", "").strip()
        self.password = os.getenv("MOLONI_PASSWORD", "").strip()
        self.company_id = _int_or_none(os.getenv("MOLONI_COMPANY_ID"))

        # What counts as an "in-store sale" (all optional; empty = no filter)
        self.document_set_id = _int_or_none(os.getenv("MOLONI_DOCUMENT_SET_ID"))
        self.document_type_id = _int_or_none(os.getenv("MOLONI_DOCUMENT_TYPE_ID"))
        self.status = _int_or_none(os.getenv("MOLONI_STATUS"))
        self.lookback_days = int(os.getenv("MOLONI_LOOKBACK_DAYS", "1"))

        # ntfy
        self.ntfy_server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        self.ntfy_topic = os.getenv("NTFY_TOPIC", "").strip()
        self.ntfy_token = os.getenv("NTFY_TOKEN", "").strip()
        self.ntfy_priority = os.getenv("NTFY_PRIORITY", "default").strip().lower()

        # General
        self.poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "120"))
        self.state_file = os.getenv("STATE_FILE", "./data/state.json")
        self.log_level = os.getenv("LOG_LEVEL", "INFO").upper()

        # Active window (local business hours); empty = always active.
        self.active_start = os.getenv("ACTIVE_START", "").strip()  # "HH:MM"
        self.active_end = os.getenv("ACTIVE_END", "").strip()      # "HH:MM"
        self.active_tz = os.getenv("ACTIVE_TZ", "Europe/Lisbon").strip()

        # Max seconds for `run` before exiting (0 = unlimited). Used in CI so the
        # job ends in time to be relaunched (the GitHub Actions "chain").
        self.max_runtime = int(os.getenv("MAX_RUNTIME_SECONDS", "0"))

        # Daily summary at SUMMARY_TIME (local, cfg.active_tz); empty disables it.
        self.summary_time = os.getenv("SUMMARY_TIME", "20:30").strip()
        self.daily_goal = float(os.getenv("DAILY_GOAL", "4200"))
        self.summary_salesmen = [
            s.strip()
            for s in os.getenv("SUMMARY_SALESMEN", "Reshma,Pajo,Rodrigo,Izadora").split(",")
            if s.strip()
        ]

    def require_moloni(self) -> None:
        missing = [
            name
            for name, value in {
                "MOLONI_CLIENT_ID": self.client_id,
                "MOLONI_CLIENT_SECRET": self.client_secret,
                "MOLONI_USERNAME": self.username,
                "MOLONI_PASSWORD": self.password,
            }.items()
            if not value
        ]
        if missing:
            raise SystemExit(
                "Faltam variaveis obrigatorias do Moloni: " + ", ".join(missing)
            )

    def require_ntfy(self) -> None:
        if not self.ntfy_topic:
            raise SystemExit("Falta NTFY_TOPIC (escolhe um topico secreto e unico).")


# --------------------------------------------------------------------------- #
# Persistent state
# --------------------------------------------------------------------------- #
class State:
    def __init__(self, path: str) -> None:
        self.path = path
        self.company_id = None
        self.access_token = None
        self.access_token_expires_at = 0.0
        self.refresh_token = None
        self.refresh_token_expires_at = 0.0
        self.last_seen_document_id = 0
        self.last_summary_date = None  # "YYYY-MM-DD" of the last daily summary sent

    @classmethod
    def load(cls, path: str) -> "State":
        state = cls(path)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                state.company_id = data.get("company_id")
                state.access_token = data.get("access_token")
                state.access_token_expires_at = data.get("access_token_expires_at", 0.0)
                state.refresh_token = data.get("refresh_token")
                state.refresh_token_expires_at = data.get("refresh_token_expires_at", 0.0)
                state.last_seen_document_id = data.get("last_seen_document_id", 0)
                state.last_summary_date = data.get("last_summary_date")
            except Exception as exc:
                LOG.warning("Nao consegui ler o estado (%s): %s", path, exc)
        return state

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "company_id": self.company_id,
                    "access_token": self.access_token,
                    "access_token_expires_at": self.access_token_expires_at,
                    "refresh_token": self.refresh_token,
                    "refresh_token_expires_at": self.refresh_token_expires_at,
                    "last_seen_document_id": self.last_seen_document_id,
                    "last_summary_date": self.last_summary_date,
                },
                fh,
                indent=2,
            )
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# Moloni API client
# --------------------------------------------------------------------------- #
class Moloni:
    def __init__(self, cfg: Config, state: State) -> None:
        self.cfg = cfg
        self.state = state
        self.session = requests.Session()
        self._salesmen = None  # lazy cache: salesman_id -> name

    # --- authentication ---------------------------------------------------- #
    def access_token(self) -> str:
        now = time.time()
        if self.state.access_token and self.state.access_token_expires_at > now + 60:
            return self.state.access_token
        if self.state.refresh_token and self.state.refresh_token_expires_at > now + 60:
            try:
                return self._grant(
                    {
                        "grant_type": "refresh_token",
                        "client_id": self.cfg.client_id,
                        "client_secret": self.cfg.client_secret,
                        "refresh_token": self.state.refresh_token,
                    }
                )
            except AuthError as exc:
                LOG.warning("Refresh falhou (%s); a autenticar com password.", exc)
        return self._grant(
            {
                "grant_type": "password",
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "username": self.cfg.username,
                "password": self.cfg.password,
            }
        )

    def reauthenticate(self) -> str:
        self.state.access_token = None
        self.state.refresh_token = None
        return self.access_token()

    def _grant(self, params: dict) -> str:
        resp = self.session.get(GRANT_URL, params=params, timeout=30)
        try:
            data = resp.json()
        except ValueError:
            raise AuthError(f"Resposta invalida do grant (HTTP {resp.status_code}).")
        if isinstance(data, dict) and data.get("error"):
            raise AuthError(f"{data.get('error')}: {data.get('error_description', '')}")
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise AuthError(f"Sem access_token na resposta: {data}")
        now = time.time()
        self.state.access_token = token
        self.state.access_token_expires_at = now + int(data.get("expires_in", 3600))
        if data.get("refresh_token"):
            self.state.refresh_token = data["refresh_token"]
            # Moloni refresh tokens last ~14 days.
            self.state.refresh_token_expires_at = now + 14 * 24 * 3600
        self.state.save()
        LOG.info("Autenticado no Moloni.")
        return token

    # --- generic call ------------------------------------------------------ #
    def call(self, endpoint: str, params: dict | None = None):
        url = f"{MOLONI_BASE_URL}/{endpoint}/"
        token = self.access_token()
        resp = self.session.post(
            url, params={"access_token": token}, data=params or {}, timeout=30
        )
        if resp.status_code in (401, 403):
            raise AuthError(f"HTTP {resp.status_code} em {endpoint}")
        try:
            data = resp.json()
        except ValueError:
            raise MoloniError(f"Resposta nao-JSON de {endpoint} (HTTP {resp.status_code}).")
        if isinstance(data, dict) and data.get("error"):
            err = str(data.get("error"))
            if err in TOKEN_ERRORS:
                raise AuthError(f"{err} em {endpoint}")
            raise MoloniError(f"{err}: {data.get('error_description', '')}")
        return data

    # --- helpers ----------------------------------------------------------- #
    def company_id(self) -> int:
        if self.cfg.company_id:
            return self.cfg.company_id
        if self.state.company_id:
            return self.state.company_id
        companies = self.call("companies/getAll")
        if not isinstance(companies, list) or not companies:
            raise MoloniError("companies/getAll nao devolveu empresas.")
        for company in companies:
            LOG.info(
                "Empresa disponivel: id=%s nome=%s",
                company.get("company_id"),
                company.get("name"),
            )
        cid = int(companies[0]["company_id"])
        self.state.company_id = cid
        self.state.save()
        LOG.info("A usar company_id=%s (define MOLONI_COMPANY_ID para fixar).", cid)
        return cid

    def documents_on_date(self, company_id: int, day_iso: str) -> list:
        docs: list = []
        offset = 0
        while True:
            params = {
                "company_id": company_id,
                "date": day_iso,
                "qty": 50,
                "offset": offset,
            }
            if self.cfg.document_set_id:
                params["document_set_id"] = self.cfg.document_set_id
            if self.cfg.status is not None:
                params["status"] = self.cfg.status
            page = self.call("documents/getAll", params)
            if not isinstance(page, list) or not page:
                break
            docs.extend(page)
            if len(page) < 50:
                break
            offset += 50
        return docs

    def recent_documents(self, company_id: int) -> list:
        docs: list = []
        today = date.today()
        for delta in range(self.cfg.lookback_days + 1):
            day = (today - timedelta(days=delta)).isoformat()
            docs.extend(self.documents_on_date(company_id, day))
        return docs

    def documents_between(self, company_id: int, start, end) -> list:
        """All documents with date in [start, end] (inclusive), start/end = date objects."""
        docs: list = []
        day = start
        while day <= end:
            docs.extend(self.documents_on_date(company_id, day.isoformat()))
            day += timedelta(days=1)
        return docs

    def document_types(self, company_id: int) -> list:
        return self._paged("documents/getAllDocumentTypes", company_id)

    def document_sets(self, company_id: int) -> list:
        return self._paged("documentSets/getAll", company_id)

    def _paged(self, endpoint: str, company_id: int) -> list:
        out: list = []
        offset = 0
        while True:
            page = self.call(
                endpoint, {"company_id": company_id, "qty": 50, "offset": offset}
            )
            if not isinstance(page, list) or not page:
                break
            out.extend(page)
            if len(page) < 50:
                break
            offset += 50
        return out

    def salesman_name(self, company_id: int, salesman_id) -> str | None:
        if not salesman_id:
            return None
        sid = int(salesman_id)
        if self._salesmen is None or sid not in self._salesmen:
            self._load_salesmen(company_id)
        return self._salesmen.get(sid)

    def _load_salesmen(self, company_id: int) -> None:
        mapping = {}
        for s in self._paged("salesmen/getAll", company_id):
            try:
                mapping[int(s["salesman_id"])] = (
                    s.get("name") or s.get("number") or ""
                ).strip()
            except (KeyError, TypeError, ValueError):
                continue
        self._salesmen = mapping


# --------------------------------------------------------------------------- #
# ntfy notifications
# --------------------------------------------------------------------------- #
def send_ntfy(cfg: Config, title: str, message: str, tags=None, priority=None) -> None:
    payload = {
        "topic": cfg.ntfy_topic,
        "title": title,
        "message": message,
        "priority": NTFY_PRIORITIES.get(priority or cfg.ntfy_priority, 3),
    }
    if tags:
        payload["tags"] = tags
    headers = {}
    if cfg.ntfy_token:
        headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
    resp = requests.post(cfg.ntfy_server, json=payload, headers=headers, timeout=30)
    if resp.status_code >= 300:
        LOG.error("Falha a enviar ntfy (HTTP %s): %s", resp.status_code, resp.text[:200])
    else:
        LOG.debug("Notificacao ntfy enviada: %s", title)


def fmt_eur(value) -> str:
    try:
        formatted = f"{float(value):,.2f}"  # e.g. 1,234.56
    except (TypeError, ValueError):
        return str(value)
    # Convert to Portuguese formatting: 1 234,56 EUR
    return formatted.replace(",", " ").replace(".", ",") + " €"


def doc_reference(doc: dict) -> str:
    set_name = (doc.get("document_set") or {}).get("name") or ""
    ref = f"{set_name} {doc.get('number')}".strip()
    return ref or f"#{doc.get('document_id')}"


def notify_sale(cfg: Config, doc: dict, salesman: str | None, daily_total: float) -> None:
    # Moloni: net_value = total COM IVA; gross_value = base sem IVA.
    total = fmt_eur(doc.get("net_value"))
    vendedor = (salesman or "").strip() or "—"
    message = (
        f"Total: {total}\n"
        f"Vendedor: {vendedor}\n"
        f"Total do dia: {fmt_eur(daily_total)}"
    )
    send_ntfy(
        cfg,
        title="\U0001f4b8 Nova Venda \U0001f4b8",
        message=message,
        tags=["money_with_wings"],
        priority=cfg.ntfy_priority,
    )
    LOG.info(
        "Notificada venda %s (vendedor=%s, total=%s, dia=%s).",
        doc_reference(doc),
        vendedor,
        total,
        fmt_eur(daily_total),
    )


# --------------------------------------------------------------------------- #
# Polling
# --------------------------------------------------------------------------- #
def matches_filters(cfg: Config, doc: dict) -> bool:
    if cfg.document_type_id and int(doc.get("document_type_id", 0)) != cfg.document_type_id:
        return False
    return True


def daily_total_for(docs: list, doc: dict) -> float:
    """Sum of net_value (com IVA) for same-day sales up to and including this doc."""
    day = str(doc.get("date", ""))[:10]
    did = int(doc.get("document_id", 0))
    return sum(
        float(d.get("net_value") or 0)
        for d in docs
        if str(d.get("date", ""))[:10] == day and int(d.get("document_id", 0)) <= did
    )


def _local_now(cfg: Config) -> "datetime":
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(cfg.active_tz))
    except Exception:
        return datetime.now()  # fallback: server local time


def _hhmm_to_minutes(value: str) -> int:
    hours, _, minutes = value.partition(":")
    return int(hours) * 60 + int(minutes or 0)


def within_active_window(cfg: Config, now: "datetime | None" = None) -> bool:
    """True if 'now' (in cfg.active_tz) is inside [active_start, active_end]."""
    if not cfg.active_start or not cfg.active_end:
        return True
    if now is None:
        now = _local_now(cfg)
    now_min = now.hour * 60 + now.minute
    start = _hhmm_to_minutes(cfg.active_start)
    end = _hhmm_to_minutes(cfg.active_end)
    if start <= end:
        return start <= now_min <= end
    return now_min >= start or now_min <= end  # overnight window


def _sum_net(docs) -> float:
    return sum(float(d.get("net_value") or 0) for d in docs)


def maybe_send_daily_summary(cfg: Config, state: State, moloni: "Moloni") -> None:
    """Once per day, at/after SUMMARY_TIME, send per-salesman + accumulated totals."""
    if not cfg.summary_time:
        return
    now = _local_now(cfg)
    summary_min = _hhmm_to_minutes(cfg.summary_time)
    now_min = now.hour * 60 + now.minute
    if now_min < summary_min or now_min > summary_min + 180:
        return  # not yet, or too late (missed window)
    today = now.date()
    if state.last_summary_date == today.isoformat():
        return  # already sent today

    cid = moloni.company_id()
    month_start = today.replace(day=1)
    week_start = today - timedelta(days=today.weekday())  # Monday
    month_docs = moloni.documents_between(cid, month_start, today)

    def day_of(doc):
        return str(doc.get("date", ""))[:10]

    day_iso, week_iso = today.isoformat(), week_start.isoformat()
    day_docs = [d for d in month_docs if day_of(d) == day_iso]
    day_total = _sum_net(day_docs)
    week_total = _sum_net(d for d in month_docs if day_of(d) >= week_iso)
    month_total = _sum_net(month_docs)

    # Per-salesman totals for today.
    named = {n.casefold(): n for n in cfg.summary_salesmen}
    totals = {n: 0.0 for n in cfg.summary_salesmen}
    outros = 0.0
    for d in day_docs:
        name = (moloni.salesman_name(cid, d.get("salesman_id")) or "").strip()
        value = float(d.get("net_value") or 0)
        if name.casefold() in named:
            totals[named[name.casefold()]] += value
        else:
            outros += value

    lines = [f"{n}: {fmt_eur(totals[n])}" for n in cfg.summary_salesmen]
    lines += ["Outros: " + fmt_eur(outros), "", "Total do dia: " + fmt_eur(day_total)]
    send_ntfy(
        cfg,
        title="\U0001f4ca Totais do dia",
        message="\n".join(lines),
        tags=["bar_chart"],
        priority=cfg.ntfy_priority,
    )

    emoji = "✅" if day_total > cfg.daily_goal else "❌"
    msg2 = (
        f"Semana: {fmt_eur(week_total)}\n"
        f"Mês: {fmt_eur(month_total)}\n"
        f"Objetivo do dia: {emoji}  ({fmt_eur(day_total)} / {fmt_eur(cfg.daily_goal)})"
    )
    send_ntfy(
        cfg,
        title="\U0001f4c8 Acumulado",
        message=msg2,
        tags=["chart_with_upwards_trend"],
        priority=cfg.ntfy_priority,
    )

    state.last_summary_date = today.isoformat()
    state.save()
    LOG.info("Resumo diario enviado (dia=%s, total=%s).", day_iso, fmt_eur(day_total))


def poll_once(cfg: Config, state: State) -> int:
    moloni = Moloni(cfg, state)

    try:
        maybe_send_daily_summary(cfg, state, moloni)
    except AuthError as exc:
        LOG.warning("Auth no resumo diario (%s); a renovar.", exc)
        try:
            moloni.reauthenticate()
            maybe_send_daily_summary(cfg, state, moloni)
        except Exception:
            LOG.exception("Falha no resumo diario (pos-reauth).")
    except Exception:
        LOG.exception("Falha no resumo diario.")

    if not within_active_window(cfg):
        LOG.info(
            "Fora do horário ativo (%s-%s %s); ciclo ignorado.",
            cfg.active_start,
            cfg.active_end,
            cfg.active_tz,
        )
        return 0

    def work():
        cid = moloni.company_id()
        return cid, moloni.recent_documents(cid)

    try:
        company_id, docs = work()
    except AuthError as exc:
        LOG.warning("Erro de autenticacao (%s); a renovar credenciais.", exc)
        moloni.reauthenticate()
        company_id, docs = work()

    docs = [d for d in docs if matches_filters(cfg, d)]

    if state.last_seen_document_id == 0:
        # First start: set the baseline so we don't replay every existing sale.
        if docs:
            state.last_seen_document_id = max(int(d.get("document_id", 0)) for d in docs)
        state.save()
        LOG.info(
            "Primeiro arranque: marcado documento #%s. So notifico vendas a partir de agora.",
            state.last_seen_document_id,
        )
        return 0

    new_docs = sorted(
        (d for d in docs if int(d.get("document_id", 0)) > state.last_seen_document_id),
        key=lambda d: int(d.get("document_id", 0)),
    )
    for doc in new_docs:
        salesman = moloni.salesman_name(company_id, doc.get("salesman_id"))
        notify_sale(cfg, doc, salesman, daily_total_for(docs, doc))
        state.last_seen_document_id = max(
            state.last_seen_document_id, int(doc.get("document_id", 0))
        )

    state.save()
    if new_docs:
        LOG.info("%d nova(s) venda(s) notificada(s).", len(new_docs))
    else:
        LOG.debug("Sem vendas novas.")
    return len(new_docs)


# --------------------------------------------------------------------------- #
# Daemon
# --------------------------------------------------------------------------- #
_STOP = False


def _handle_signal(signum, _frame):
    global _STOP
    _STOP = True
    LOG.info("Sinal %s recebido; a terminar...", signum)


def run_forever(cfg: Config) -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    LOG.info(
        "Monitorizacao iniciada (intervalo=%ss, ntfy=%s, topico=%s, max_runtime=%ss).",
        cfg.poll_interval,
        cfg.ntfy_server,
        cfg.ntfy_topic,
        cfg.max_runtime or "ilimitado",
    )
    state = State.load(cfg.state_file)
    started = time.time()

    def time_up() -> bool:
        return bool(cfg.max_runtime) and (time.time() - started) >= cfg.max_runtime

    while not _STOP and not time_up():
        try:
            poll_once(cfg, state)
        except Exception as exc:  # keep the daemon alive on transient errors
            LOG.exception("Erro no ciclo de polling: %s", exc)
        for _ in range(cfg.poll_interval):  # responsive sleep for fast shutdown
            if _STOP or time_up():
                break
            time.sleep(1)
    if time_up():
        LOG.info("MAX_RUNTIME_SECONDS atingido; a sair para relancar a corrente.")
    LOG.info("Monitorizacao terminada.")


# --------------------------------------------------------------------------- #
# One-shot commands
# --------------------------------------------------------------------------- #
def cmd_check(cfg: Config) -> None:
    cfg.require_moloni()
    state = State.load(cfg.state_file)
    moloni = Moloni(cfg, state)
    moloni.access_token()
    cid = moloni.company_id()
    docs = moloni.recent_documents(cid)
    print(
        f"OK: autenticado, company_id={cid}, {len(docs)} documento(s) na janela de "
        f"{cfg.lookback_days + 1} dia(s)."
    )
    print(
        f"Filtros -> document_set_id={cfg.document_set_id}, "
        f"document_type_id={cfg.document_type_id}, status={cfg.status}"
    )
    print(
        f"ntfy -> servidor={cfg.ntfy_server}, "
        f"topico={'(definido)' if cfg.ntfy_topic else '(EM FALTA)'}"
    )


def cmd_list_types(cfg: Config) -> None:
    cfg.require_moloni()
    state = State.load(cfg.state_file)
    moloni = Moloni(cfg, state)
    cid = moloni.company_id()

    print("\n=== Tipos de documento (MOLONI_DOCUMENT_TYPE_ID) ===")
    for t in moloni.document_types(cid):
        print(
            f"  id={str(t.get('document_type_id')):>4}  {t.get('name')}  "
            f"(saft={t.get('saft_code')})"
        )

    print("\n=== Series / conjuntos (MOLONI_DOCUMENT_SET_ID) ===")
    for s in moloni.document_sets(cid):
        print(f"  id={str(s.get('document_set_id')):>4}  {s.get('name')}")

    print("\n=== Ultimos documentos (para identificares a 'venda em loja') ===")
    docs = sorted(
        moloni.recent_documents(cid),
        key=lambda d: int(d.get("document_id", 0)),
        reverse=True,
    )[:15]
    for d in docs:
        print(
            f"  doc_id={d.get('document_id')}  tipo={d.get('document_type_id')}  "
            f"serie={(d.get('document_set') or {}).get('name')}  "
            f"status={d.get('status')}  {d.get('date')}  "
            f"{d.get('entity_name')}  {fmt_eur(d.get('net_value'))}"
        )
    print(
        "\nDica: define MOLONI_DOCUMENT_TYPE_ID e/ou MOLONI_DOCUMENT_SET_ID e "
        "MOLONI_STATUS com base nas linhas acima."
    )


def cmd_test_notify(cfg: Config) -> None:
    cfg.require_ntfy()
    send_ntfy(
        cfg,
        title="✅ Teste Moloni -> ntfy",
        message="Se recebeste isto, as notificacoes estao a funcionar.",
        tags=["white_check_mark"],
        priority=cfg.ntfy_priority,
    )
    print(f"Notificacao de teste enviada para {cfg.ntfy_server} (topico {cfg.ntfy_topic}).")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    _load_env_file()
    cfg = Config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="Notifica vendas do Moloni via ntfy.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in [
        ("run", "Corre em continuo (cloud/daemon)."),
        ("once", "Faz um ciclo e sai (cron/serverless)."),
        ("check", "Valida configuracao e ligacao ao Moloni."),
        ("list-types", "Mostra tipos/series/documentos para configurar os filtros."),
        ("test-notify", "Envia uma notificacao de teste pelo ntfy."),
    ]:
        sub.add_parser(name, help=help_text)

    args = parser.parse_args(argv)

    if args.command == "run":
        cfg.require_moloni()
        cfg.require_ntfy()
        run_forever(cfg)
    elif args.command == "once":
        cfg.require_moloni()
        cfg.require_ntfy()
        poll_once(cfg, State.load(cfg.state_file))
    elif args.command == "check":
        cmd_check(cfg)
    elif args.command == "list-types":
        cmd_list_types(cfg)
    elif args.command == "test-notify":
        cmd_test_notify(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
