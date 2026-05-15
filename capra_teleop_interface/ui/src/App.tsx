import { Tab, TabId, Tabs } from "@blueprintjs/core";
import { useCallback, useEffect, useRef, useState } from "react";

import { getState, setControlActive, TeleopState } from "./api";
import { ControlTab } from "./tabs/ControlTab";
import { DataTab } from "./tabs/DataTab";
import { SettingsTab } from "./tabs/SettingsTab";

// Adding a new tab is one entry here + a component. Anything that needs
// "this tab is active" can subscribe via the `activeTab` prop pattern the
// existing tabs use.
const TAB_DEFS: { id: string; title: string; component: React.FC<TabProps> }[] = [
  { id: "control",  title: "Control",  component: ControlTab },
  { id: "data",     title: "Data",     component: DataTab },
  { id: "settings", title: "Settings", component: SettingsTab },
];

export interface TabProps {
  /** True when this tab is the visible one — tabs use this to start/stop
      polling, open subscriptions, etc. so a hidden tab does no work. */
  active: boolean;
  /** Latest teleop /state snapshot (or null while connecting). */
  state: TeleopState | null;
}

export function App() {
  const [tab, setTab] = useState<TabId>("control");
  const [state, setState] = useState<TeleopState | null>(null);
  const [conn, setConn] = useState<"connecting" | "live" | "offline">("connecting");
  const [connError, setConnError] = useState<string>("");

  // --- Poll /state -----------------------------------------------------------
  // 250ms matches what the legacy UI was doing. Pulled out into a stable
  // callback so the polling effect doesn't rebuild on every render.
  const tick = useCallback(async () => {
    try {
      const s = await getState();
      setState(s);
      setConn("live");
      setConnError("");
    } catch (e) {
      setConn("offline");
      setConnError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void tick();
    const h = window.setInterval(tick, 250);
    return () => window.clearInterval(h);
  }, [tick]);

  // --- Tell the backend whether the Control tab is selected ----------------
  // The Python send loop only forwards commands when control_active=true so
  // the operator can't accidentally drive the robot while fiddling with
  // Settings. Always sync on tab change AND on app mount.
  const lastSent = useRef<boolean | null>(null);
  useEffect(() => {
    const want = tab === "control";
    if (lastSent.current === want) return;
    lastSent.current = want;
    setControlActive(want).catch(() => {
      // Worst case the gate fails-closed (already the server's default).
      lastSent.current = null;
    });
  }, [tab]);

  // --- Space-bar E-stop (legacy muscle memory) -----------------------------
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === " " || e.code === "Space") {
        e.preventDefault();
        // Fire and forget — ControlTab also exposes a button.
        void fetch("/estop", { method: "POST" });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="app">
      {state?.estopped && (
        <div className="app__banner-estop">
          E-STOP ENGAGED — outbound commands zeroed
        </div>
      )}

      <div className="app__header">
        <span className="app__title">capra teleop</span>
        <span className={`app__status ${conn === "live" ? "good" : conn === "offline" ? "bad" : ""}`}>
          {conn === "live" && "live"}
          {conn === "offline" && `offline (${connError})`}
          {conn === "connecting" && "connecting…"}
        </span>
      </div>

      <div className="app__tabs">
        <Tabs id="main-tabs" selectedTabId={tab} onChange={setTab} large>
          {TAB_DEFS.map((t) => (
            <Tab id={t.id} key={t.id} title={t.title} />
          ))}
        </Tabs>
      </div>

      <div className="app__content">
        {TAB_DEFS.map((t) => {
          const Component = t.component;
          // Keep all panels mounted so per-tab state survives switches;
          // hide the inactive ones via display:none. `active` is the
          // contract each tab uses to gate side effects.
          return (
            <div key={t.id} style={{ display: tab === t.id ? "block" : "none" }}>
              <Component active={tab === t.id} state={state} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
