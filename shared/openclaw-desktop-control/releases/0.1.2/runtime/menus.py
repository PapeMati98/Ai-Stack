"""Owner menu: target buttons outside the raw prompt. Initial menu once."""

from __future__ import annotations

from typing import Any

from openclaw_control import models


MENU_TEXT = (
    "OpenClaw: wybierz cel przyciskiem, potem wyslij surową tresc. "
    "STOP wstrzymuje nowe wysylki."
)


def target_rows(store: Any, cfg: Any, now: float, dispatch_id='') -> list:
    rows = []
    for spec in cfg.targets:
        if spec.enabled and spec.verified and spec.native_thread:
            action = store.issue_callback(
                kind="target_select",
                task_id=spec.task_id,
                provider=spec.provider,
                native_thread=spec.native_thread,
                generation=spec.generation,
                scope=('dispatch:' + dispatch_id) if dispatch_id else '',
                now=now,
            )
            rows.append(
                [{"text": spec.label[:32], "callback_data": action}]
            )
        else:
            action = store.issue_callback(
                kind="disabled_target",
                task_id=spec.task_id or spec.id,
                provider=spec.provider,
                native_thread=spec.native_thread,
                generation=spec.generation,
                scope="",
                now=now,
            )
            label = (spec.label or spec.id or "cel")[:28] + " (wylaczony)"
            rows.append([{"text": label, "callback_data": action}])
    if not rows:
        action = store.issue_callback(
            kind="disabled_target",
            task_id="",
            provider="",
            native_thread="",
            generation=0,
            scope="",
            now=now,
        )
        rows.append(
            [{"text": "brak celu (wylaczony)", "callback_data": action}]
        )
    stop = store.issue_callback(
        kind="stop",
        task_id="",
        provider="",
        native_thread="",
        generation=0,
        scope=models.STOP_SCOPE_ALL,
        now=now,
    )
    rows.append([{"text": "STOP", "callback_data": stop}])
    if store.is_stopped():
        resume = store.issue_callback(kind='resume', task_id='', provider='', native_thread='',
                                      generation=0, scope=models.STOP_SCOPE_ALL, now=now)
        rows.append([{'text': 'Wznów wysyłki', 'callback_data': resume}])
    show = store.issue_callback(
        kind="show_menu",
        task_id="",
        provider="",
        native_thread="",
        generation=0,
        scope="",
        now=now,
    )
    rows.append([{"text": "Menu", "callback_data": show}])
    return rows


def keyboard(store: Any, cfg: Any, now: float, dispatch_id='') -> dict:
    return {"inline_keyboard": target_rows(store, cfg, now, dispatch_id)}
