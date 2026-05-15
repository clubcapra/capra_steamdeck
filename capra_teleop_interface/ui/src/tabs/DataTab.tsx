import { Callout, Card, Spinner, Tag } from "@blueprintjs/core";
import { useEffect, useState } from "react";

import {
  getSensorState,
  listSensors,
  SensorLive,
  SensorSummary,
  setSensorSubscriptions,
} from "../api";
import type { TabProps } from "../App";

const fmt = (v: unknown): string => {
  if (v == null) return "—";
  if (typeof v === "number") return Number.isFinite(v) ? v.toFixed(3) : String(v);
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
};

export function DataTab({ active }: TabProps) {
  const [summaries, setSummaries] = useState<SensorSummary[]>([]);
  const [live, setLive] = useState<Record<string, SensorLive>>({});
  const [err, setErr] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(true);

  // Discover once on entry, and refresh whenever the tab is activated.
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        const { sensors } = await listSensors();
        if (cancelled) return;
        setSummaries(sensors);
        // Subscribe to everything we discovered. The backend tracks current
        // subscriptions per id; on tab leave the unmount-effect drops them.
        await setSensorSubscriptions(sensors.map((s) => s.id));
        setErr("");
      } catch (e) {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [active]);

  // While the tab is active, poll the live state at 4 Hz.
  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    const pull = async () => {
      try {
        const { sensors } = await getSensorState();
        if (cancelled) return;
        const map: Record<string, SensorLive> = {};
        for (const s of sensors) map[s.id] = s;
        setLive(map);
      } catch (e) {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      }
    };
    void pull();
    const h = window.setInterval(pull, 250);
    return () => { cancelled = true; window.clearInterval(h); };
  }, [active]);

  // On tab leave, tell the server to drop all subscriptions so we're not
  // keeping a bunch of UDP sockets open in the background.
  useEffect(() => {
    if (active) return;
    setSensorSubscriptions([]).catch(() => { /* best effort */ });
  }, [active]);

  if (err) {
    return <Callout intent="danger" icon="error">Could not load sensors: {err}</Callout>;
  }
  if (loading && summaries.length === 0) {
    return <Spinner />;
  }
  if (summaries.length === 0) {
    return <Callout intent="warning" icon="info-sign">No sensors discovered.</Callout>;
  }

  return (
    <div className="grid-2">
      {summaries.map((s) => {
        const l = live[s.id];
        return (
          <Card key={s.id}>
            <div style={{ display: "flex", alignItems: "baseline", gap: "0.5em" }}>
              <h3 style={{ margin: 0 }}>{s.display_name ?? s.id}</h3>
              <Tag minimal>{s.id}</Tag>
              {l?.rate_hz != null && (
                <Tag minimal intent="primary">{l.rate_hz.toFixed(1)} Hz</Tag>
              )}
              {l?.last_packet_age_s != null && l.last_packet_age_s > 2 && (
                <Tag minimal intent="warning">stale {l.last_packet_age_s.toFixed(1)}s</Tag>
              )}
            </div>
            {l?.last_error && (
              <Callout intent="warning" icon="warning-sign" style={{ marginTop: "0.5em" }}>
                {l.last_error}
              </Callout>
            )}
            <div style={{ marginTop: "0.5em" }}>
              {l && Object.keys(l.latest).length > 0 ? (
                Object.entries(l.latest).map(([k, v]) => (
                  <div className="row" key={k}>
                    <span className="label">{k}</span>
                    <span className="value">{fmt(v)}</span>
                  </div>
                ))
              ) : (
                <div className="dim">waiting for data…</div>
              )}
            </div>
          </Card>
        );
      })}
    </div>
  );
}
