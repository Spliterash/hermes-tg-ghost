(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;
  const { React } = SDK;
  const h = React.createElement;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const { Card, CardContent, CardHeader, CardTitle, Badge, Button } = SDK.components;

  const API = "/api/plugins/tg-ghost/status";

  function formatTime(seconds) {
    return seconds ? new Date(seconds * 1000).toLocaleString() : "—";
  }

  function Row(props) {
    return h("div", { style: { display: "flex", justifyContent: "space-between", padding: "4px 0", fontSize: 13 } },
      h("span", { className: "text-muted-foreground" }, props.label),
      h("span", null, props.value));
  }

  function userbotBadge(ub) {
    const last = ub.last_status;
    if (!ub.configured) return "not configured";
    if (!ub.session_exists) return "no session";
    if (!last) return "not used yet";
    return last.state === "connected" ? "connected" : "error";
  }

  function GuestModePage() {
    const [state, setState] = useState({ loading: true, data: null, err: null });
    const load = useCallback(function () {
      setState({ loading: true, data: null, err: null });
      fetch(API).then(function (r) {
        if (!r.ok) throw new Error(r.status + ": " + r.statusText);
        return r.json();
      }).then(function (data) {
        setState({ loading: false, data: data, err: null });
      }).catch(function (e) {
        setState({ loading: false, data: null, err: String((e && e.message) || e) });
      });
    }, []);
    useEffect(function () { load(); }, [load]);

    if (state.loading) return h("p", { className: "text-sm text-muted-foreground" }, "Loading Telegram Ghost status…");
    if (state.err) return h("div", null,
      h("p", { className: "text-sm text-destructive" }, "Failed: " + state.err),
      h(Button, { size: "sm", onClick: load }, "Retry"));

    const ub = state.data.userbot;
    const last = ub.last_status || {};
    const sessions = state.data.sessions;
    return h("div", { style: { display: "grid", gap: 12, maxWidth: 860 } },
      h(Card, null,
        h(CardHeader, null, h(CardTitle, null, "Userbot")),
        h(CardContent, null,
          h(Row, { label: "State", value: h(Badge, null, userbotBadge(ub)) }),
          last.user ? h(Row, { label: "Account", value: last.user }) : null,
          last.error ? h(Row, { label: "Last error", value: last.error }) : null,
          h(Row, { label: "Last update", value: formatTime(last.at) }),
          h("div", { style: { marginTop: 8 } }, h(Button, { size: "sm", onClick: load }, "Refresh")))),
      h(Card, null,
        h(CardHeader, null, h(CardTitle, null, "Ghost sessions (" + sessions.length + ")")),
        h(CardContent, null,
          sessions.length ? sessions.map(function (s) {
            return h("div", { key: s.id, style: { padding: "4px 0", fontSize: 13, borderBottom: "1px solid var(--color-border)" } },
              h("div", null, s.title || s.session_key || s.id),
              h("div", { className: "text-muted-foreground" },
                (s.message_count || 0) + " msgs · " + formatTime(s.last_active) + (s.ended_at ? " · ended" : "")));
          }) : h("p", { className: "text-sm text-muted-foreground" }, "No ghost sessions yet."))));
  }

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("tg-ghost", GuestModePage);
  }
})();
