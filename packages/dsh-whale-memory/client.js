/* 记忆星图：侧栏图标 + 内嵌记忆银河（dsh-memory-galaxy 的 /memory-galaxy/ 页面） */
window.__ModuleLoader__.load({
  id: "dsh-whale-memory",
  factory(require) {
    const React = require("react");
    const h = React.createElement;

    const PANEL_ID = "whale-memory";
    const SRC = "/memory-galaxy/";

    function Panel() {
      const [url, setUrl] = React.useState(SRC);
      return h(
        "div",
        {
          style: {
            width: "100%",
            height: "100%",
            minHeight: "80vh",
            display: "flex",
            flexDirection: "column",
            gap: 8,
            padding: "8px 4px 4px",
            boxSizing: "border-box",
          },
        },
        h(
          "div",
          {
            style: {
              display: "flex",
              alignItems: "center",
              gap: 10,
              fontSize: 12,
              opacity: 0.75,
              padding: "0 6px",
            },
          },
          h("span", null, "记忆银河"),
          h(
            "a",
            {
              href: SRC,
              target: "_blank",
              rel: "noreferrer",
              style: { color: "inherit", textDecoration: "underline", opacity: 0.8 },
            },
            "新窗口打开"
          ),
          h(
            "button",
            {
              onClick: () => setUrl(SRC + "?t=" + Date.now()),
              style: {
                marginLeft: "auto",
                border: "1px solid currentColor",
                background: "transparent",
                color: "inherit",
                borderRadius: 6,
                padding: "2px 8px",
                cursor: "pointer",
                fontSize: 12,
                opacity: 0.8,
              },
            },
            "刷新"
          )
        ),
        h("iframe", {
          key: url,
          src: url,
          title: "memory-galaxy",
          style: {
            flex: 1,
            width: "100%",
            minHeight: "72vh",
            border: "0",
            borderRadius: 12,
            background: "transparent",
          },
        })
      );
    }

    function Icon() {
      return h(
        "svg",
        { width: 18, height: 18, viewBox: "0 0 24 24", fill: "none" },
        h("ellipse", {
          cx: 12,
          cy: 12,
          rx: 10,
          ry: 4.4,
          stroke: "currentColor",
          strokeWidth: 1.3,
          opacity: 0.55,
          transform: "rotate(-24 12 12)",
        }),
        h("ellipse", {
          cx: 12,
          cy: 12,
          rx: 6.4,
          ry: 2.8,
          stroke: "currentColor",
          strokeWidth: 1.2,
          opacity: 0.75,
          transform: "rotate(-24 12 12)",
        }),
        h("circle", { cx: 12, cy: 12, r: 2.1, fill: "currentColor" }),
        h("circle", { cx: 19.2, cy: 8.2, r: 1.05, fill: "currentColor", opacity: 0.9 }),
        h("circle", { cx: 5.1, cy: 16.1, r: 0.85, fill: "currentColor", opacity: 0.7 })
      );
    }

    function apply(ctx) {
      ctx.slots.inject("sidebar.panellist", () => {
        const stopIcon = ctx.slots.register(
          {
            name: "sidebar.panellist",
            id: PANEL_ID,
            order: 30,
            label: () => "记忆星图",
          },
          Icon
        );
        return () => stopIcon();
      });

      ctx.slots.inject("main", () => {
        const stopMain = ctx.slots.register({ name: "main", key: PANEL_ID }, Panel);
        return () => stopMain();
      });
    }

    return { inject: ["slots"], apply };
  },
});
