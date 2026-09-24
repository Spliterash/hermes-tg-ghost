(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;
  const { React } = SDK;
  const h = React.createElement;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const { Card, CardContent, CardHeader, CardTitle, Badge, Button, Input } = SDK.components;

  const API = "/api/plugins/tg-ghost";

  // fetchJSON throws Error("<status>: <body>"); FastAPI bodies are {"detail": "..."}.
  function errorText(err) {
    const raw = (err && err.message) ? String(err.message) : String(err || "");
    const m = raw.match(/^(\d{3}):\s*(.*)$/s);
    try { return JSON.parse(m ? m[2] : raw).detail || raw; } catch (_) { return m ? m[2] || raw : raw; }
  }

  function post(path, body) {
    return SDK.fetchJSON(API + path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
    });
  }

  function formatTime(seconds) {
    return seconds ? new Date(seconds * 1000).toLocaleString() : "—";
  }

  function Row(props) {
    return h("div", { style: { display: "flex", justifyContent: "space-between", gap: 12, padding: "4px 0", fontSize: 13 } },
      h("span", { className: "text-muted-foreground" }, props.label),
      h("span", { style: { textAlign: "right" } }, props.value));
  }

  function yesNo(ok, yes, no) {
    return h(Badge, { variant: ok ? "default" : "destructive" }, ok ? yes : no);
  }

  function PlatformCard(props) {
    const p = props.platform;
    const connected = !!p && p.state === "connected";
    return h(Card, null,
      h(CardHeader, null, h(CardTitle, null, "Гостевой режим")),
      h(CardContent, null,
        h(Row, { label: "Платформа telegram_ghost", value: yesNo(connected, "подключена", p ? "отключена" : "не запускалась") }),
        p ? h(Row, { label: "Обновлено", value: formatTime(p.at) }) : null,
        connected ? null : h("p", { className: "text-sm text-muted-foreground", style: { marginTop: 8 } },
          "Платформа поднимается вместе с основным Telegram-адаптером: нужен токен бота, включённый плагин и перезапуск гейтвея.")));
  }

  function LoginForm(props) {
    const [step, setStep] = useState(props.initialStep || "phone");
    const [value, setValue] = useState("");
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState(null);

    const labels = {
      phone: ["Телефон аккаунта юзербота", "+79990000000", "Получить код"],
      code: ["Код из Telegram", "12345", "Войти"],
      password: ["Пароль 2FA", "", "Войти"],
    };
    const [label, placeholder, action] = labels[step];

    function submit() {
      setBusy(true); setErr(null);
      const req = step === "phone" ? post("/login/start", { phone: value })
        : step === "code" ? post("/login/code", { code: value })
        : post("/login/password", { password: value });
      req.then(function (res) {
        setValue("");
        if (res.step === "done") props.onDone();
        else setStep(res.step);
      }).catch(function (e) { setErr(errorText(e)); }).finally(function () { setBusy(false); });
    }

    function cancel() {
      post("/login/cancel").finally(function () { setStep("phone"); setValue(""); setErr(null); props.onCancel(); });
    }

    return h("div", { style: { display: "grid", gap: 8, marginTop: 8 } },
      h("span", { style: { fontSize: 13 } }, label),
      h(Input, {
        type: step === "password" ? "password" : "text", value: value, placeholder: placeholder, disabled: busy,
        onChange: function (e) { setValue(e.target.value); },
        onKeyDown: function (e) { if (e.key === "Enter" && value) submit(); },
      }),
      err ? h("p", { className: "text-sm text-destructive" }, err) : null,
      h("div", { style: { display: "flex", gap: 8 } },
        h(Button, { size: "sm", disabled: busy || !value, onClick: submit }, action),
        h(Button, { size: "sm", variant: "outline", disabled: busy, onClick: cancel }, "Отмена")));
  }

  function UserbotCard(props) {
    const ub = props.userbot;
    const last = ub.last_status || {};
    const [login, setLogin] = useState(!!ub.login_step);
    const ready = ub.telethon && ub.configured;
    return h(Card, null,
      h(CardHeader, null, h(CardTitle, null, "Юзербот")),
      h(CardContent, null,
        h(Row, { label: "Telethon", value: yesNo(ub.telethon, "установлен", "не установлен") }),
        h(Row, { label: "api_id / api_hash", value: yesNo(ub.configured, "заданы", "не заданы") }),
        h(Row, { label: "Сессия", value: yesNo(ub.session_exists, "есть", "нет") }),
        last.user ? h(Row, { label: "Аккаунт", value: last.user }) : null,
        last.state ? h(Row, { label: "Последнее состояние", value: last.state + " · " + formatTime(last.at) }) : null,
        last.error ? h(Row, { label: "Последняя ошибка", value: last.error }) : null,
        ub.configured ? null : h("p", { className: "text-sm text-muted-foreground", style: { marginTop: 8 } },
          "api_id и api_hash задаются в настройках плагина (my.telegram.org)."),
        login
          ? h(LoginForm, {
              initialStep: ub.login_step,
              onDone: function () { setLogin(false); props.reload(); },
              onCancel: function () { setLogin(false); },
            })
          : h("div", { style: { display: "flex", gap: 8, marginTop: 8 } },
              h(Button, { size: "sm", disabled: !ready, onClick: function () { setLogin(true); } },
                ub.session_exists ? "Войти заново" : "Войти"),
              h(Button, { size: "sm", variant: "outline", onClick: props.reload }, "Обновить"))));
  }

  function SessionsCard(props) {
    const sessions = props.sessions;
    return h(Card, null,
      h(CardHeader, null, h(CardTitle, null, "Гостевые сессии (" + sessions.length + ")")),
      h(CardContent, null,
        sessions.length ? sessions.map(function (s) {
          return h("div", { key: s.id, style: { padding: "4px 0", fontSize: 13, borderBottom: "1px solid var(--color-border)" } },
            h("div", null, s.title || s.session_key || s.id),
            h("div", { className: "text-muted-foreground" },
              (s.message_count || 0) + " сообщ. · " + formatTime(s.last_active) + (s.ended_at ? " · завершена" : "")));
        }) : h("p", { className: "text-sm text-muted-foreground" }, "Гостевых сессий пока нет.")));
  }

  function GhostPage() {
    const [state, setState] = useState({ loading: true, data: null, err: null });
    const load = useCallback(function () {
      SDK.fetchJSON(API + "/status").then(function (data) {
        setState({ loading: false, data: data, err: null });
      }).catch(function (e) {
        setState({ loading: false, data: null, err: errorText(e) });
      });
    }, []);
    useEffect(function () { load(); }, [load]);

    if (state.loading) return h("p", { className: "text-sm text-muted-foreground" }, "Загрузка…");
    if (state.err) return h("div", null,
      h("p", { className: "text-sm text-destructive" }, "Ошибка: " + state.err),
      h(Button, { size: "sm", onClick: load }, "Повторить"));

    const data = state.data;
    return h("div", { style: { display: "grid", gap: 12, maxWidth: 860 } },
      h(PlatformCard, { platform: data.platform }),
      h(UserbotCard, { userbot: data.userbot, reload: load }),
      h(SessionsCard, { sessions: data.sessions }));
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("tg-ghost", GhostPage);
  }
})();
