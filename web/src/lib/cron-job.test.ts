import { describe, expect, it } from "vitest";

import {
  buildCronJobPayload,
  cronJobHasExecutionContent,
  cronJobFormFromJob,
  cronLastResult,
  cronTargetSupportsInternalTurn,
  cronAgoLabel,
  cronNextRunOverdueMs,
  cronSchedulerStaleAgeS,
  splitCronList,
  type CronJobFormState,
} from "./cron-job";
import type { CronJob } from "./api";

function form(overrides: Partial<CronJobFormState> = {}): CronJobFormState {
  return {
    name: "",
    prompt: "prompt",
    schedule: "every 1h",
    deliver: "local",
    skills: [],
    provider: "",
    model: "",
    base_url: "",
    script: "",
    no_agent: false,
    job_type: "agent",
    delivery_mode: "direct",
    delivery_target: "",
    attach_to_session: null,
    context_from: "",
    continuity: false,
    enabled_toolsets: [],
    workdir: "",
    ...overrides,
  };
}

describe("splitCronList", () => {
  it("normalizes comma and newline separated cron list fields", () => {
    expect(splitCronList(" web, terminal\nfile ,, ")).toEqual([
      "web",
      "terminal",
      "file",
    ]);
  });
});

describe("reminder and canonical delivery controls", () => {
  it("preserves literal text and serializes execution independently from delivery", () => {
    const payload = buildCronJobPayload(form({ prompt: "  remember\n", job_type: "reminder", deliver: "origin", delivery_mode: "internal_turn" }));
    expect(payload).toMatchObject({ prompt: "  remember\n", job_type: "reminder", no_agent: false, delivery_mode: "internal_turn" });
    expect(buildCronJobPayload(form({ job_type: "script", script: "watch.py" })).no_agent).toBe(true);
  });

  it("roundtrips canonical targets and applies an explicit attachment choice", () => {
    const original: CronJob = { id: "job", enabled: true, job_type: "reminder", delivery_mode: "internal_turn",
      delivery_target: { kind: "session", selector: "discord:dm:42", attach: false, wake_policy: "immediate" } };
    const state = cronJobFormFromJob(original);
    expect(buildCronJobPayload(state).delivery_target).toEqual(original.delivery_target);
    expect(buildCronJobPayload({ ...state, attach_to_session: true }).delivery_target).toMatchObject({ attach: true });
  });

  it("keeps legacy script aliases and distinguishes unset attachment from false", () => {
    const state = cronJobFormFromJob({ id: "old", enabled: true, no_agent: true });
    expect(state.job_type).toBe("script");
    expect(state.delivery_mode).toBe("direct");
    expect(buildCronJobPayload(state)).not.toHaveProperty("attach_to_session");
    expect(buildCronJobPayload({ ...state, attach_to_session: false }).attach_to_session).toBe(false);
  });

  it("rejects invalid canonical JSON and identifies single conversation destinations", () => {
    expect(() => buildCronJobPayload(form({ delivery_target: "not json" }))).toThrow();
    expect(() => buildCronJobPayload(form({ delivery_target: "[]" }))).toThrow();
    expect(cronTargetSupportsInternalTurn("origin")).toBe(true);
    expect(cronTargetSupportsInternalTurn("bot-chat")).toBe(true);
    for (const target of ["local", "all", "discord,telegram", "bot-chat:other", "room:42"]) {
      expect(cronTargetSupportsInternalTurn(target)).toBe(false);
    }
  });
});

describe("buildCronJobPayload", () => {
  it("normalizes list fields and base URLs", () => {
    const payload = buildCronJobPayload(
      form({
        base_url: "https://example.invalid/v1/",
        enabled_toolsets: ["web", ""],
        context_from: "upstream-a\nupstream-b",
      }),
    );

    expect(payload).toMatchObject({
      base_url: "https://example.invalid/v1",
      context_from: ["upstream-a", "upstream-b"],
      enabled_toolsets: ["web"],
    });
  });

  it("stores continuity as the reserved self entry", () => {
    const payload = buildCronJobPayload(
      form({ continuity: true, context_from: "upstream-a" }),
    );

    expect(payload.context_from).toEqual(["upstream-a", "self"]);
  });

  it("continuity off strips any hand-typed self entry", () => {
    const payload = buildCronJobPayload(
      form({ continuity: false, context_from: "SELF\nupstream-a" }),
    );

    expect(payload.context_from).toEqual(["upstream-a"]);
  });

  it("keeps clear operations explicit for update payloads", () => {
    const payload = buildCronJobPayload(form({ schedule: "every 2h" }));

    expect(payload).toMatchObject({
      schedule: "every 2h",
      provider: null,
      model: null,
      base_url: null,
      script: null,
      no_agent: false,
      context_from: null,
      enabled_toolsets: null,
      workdir: null,
    });
  });
});

describe("cronJobHasExecutionContent", () => {
  it("treats a script as execution content for agent-backed cron jobs", () => {
    const payload = buildCronJobPayload(
      form({ prompt: "", skills: [], script: "collect-status.py" }),
    );

    expect(cronJobHasExecutionContent(payload)).toBe(true);
  });

  it("rejects payloads with no prompt, skills, or script", () => {
    const payload = buildCronJobPayload(form({ prompt: "", skills: [], script: "" }));

    expect(cronJobHasExecutionContent(payload)).toBe(false);
  });
});

describe("cronJobFormFromJob", () => {
  it("preserves schedule fallback and editable list fields", () => {
    const job: CronJob = {
      id: "abc",
      enabled: true,
      schedule_display: "every 1h",
      context_from: ["upstream-a", "upstream-b"],
      enabled_toolsets: ["web"],
    };

    expect(cronJobFormFromJob(job)).toMatchObject({
      schedule: "every 1h",
      context_from: "upstream-a\nupstream-b",
      continuity: false,
      enabled_toolsets: ["web"],
    });
  });

  it("splits the stored self entry into the continuity toggle", () => {
    const job: CronJob = {
      id: "abc",
      enabled: true,
      schedule_display: "every 1h",
      context_from: ["self", "upstream-a"],
    };

    expect(cronJobFormFromJob(job)).toMatchObject({
      context_from: "upstream-a",
      continuity: true,
    });
  });

  it("prefers one-shot run_at over the human display string", () => {
    const job: CronJob = {
      id: "once-job",
      enabled: true,
      schedule: {
        kind: "once",
        run_at: "2026-02-03T14:00:00+08:00",
      },
      schedule_display: "once at 2026-02-03 14:00",
    };

    expect(cronJobFormFromJob(job)).toMatchObject({
      schedule: "2026-02-03T14:00:00+08:00",
    });
  });
});

describe("cronLastResult", () => {
  it("renders nothing for a job that never ran", () => {
    expect(cronLastResult({ last_status: null })).toBeNull();
    expect(cronLastResult({ last_status: "" })).toBeNull();
  });

  it("is green for ok with no detail", () => {
    expect(cronLastResult({ last_status: "ok", last_error: null })).toEqual({
      status: "ok",
      tone: "success",
      detail: null,
    });
  });

  it("is amber for delivery_failed and explains it from last_delivery_error", () => {
    // The agent run succeeded (last_error is null for these runs); the reason
    // lives in last_delivery_error. Must never render as green or as "unknown".
    expect(
      cronLastResult({
        last_status: "delivery_failed",
        last_error: null,
        last_delivery_error: "telegram: 502 Bad Gateway",
      }),
    ).toEqual({
      status: "delivery_failed",
      tone: "warning",
      detail: "telegram: 502 Bad Gateway",
    });
  });

  it("is red for error and any unrecognised literal", () => {
    expect(cronLastResult({ last_status: "error", last_error: "boom" })).toEqual({
      status: "error",
      tone: "destructive",
      detail: "boom",
    });
    expect(cronLastResult({ last_status: "something_new" })?.tone).toBe("destructive");
  });

  it("is amber for blocked_config (preflight refused to burn a run)", () => {
    expect(
      cronLastResult({ last_status: "blocked_config", last_error: "missing API key" }),
    ).toEqual({ status: "blocked_config", tone: "warning", detail: "missing API key" });
  });
});

describe("cronNextRunOverdueMs", () => {
  const now = Date.parse("2026-09-17T20:35:00+04:00");

  it("flags an active job whose stored slot sits past the scheduler grace (#114309)", () => {
    const job = { next_run_at: "2026-09-17T13:34:18+04:00", enabled: true, state: "scheduled" };
    expect(cronNextRunOverdueMs(job, now)).toBe(now - Date.parse(job.next_run_at));
  });

  it("keeps upcoming, within-grace, paused and unparseable slots as plain next runs", () => {
    expect(cronNextRunOverdueMs({ next_run_at: "2026-09-17T21:00:00+04:00", enabled: true }, now)).toBeNull();
    expect(cronNextRunOverdueMs({ next_run_at: "2026-09-17T20:30:00+04:00", enabled: true }, now)).toBeNull();
    expect(
      cronNextRunOverdueMs({ next_run_at: "2026-09-17T13:34:18+04:00", enabled: true, state: "paused" }, now),
    ).toBeNull();
    expect(cronNextRunOverdueMs({ next_run_at: "2026-09-17T13:34:18+04:00", enabled: false }, now)).toBeNull();
    expect(cronNextRunOverdueMs({ next_run_at: "not-a-date", enabled: true }, now)).toBeNull();
  });
});

describe("cronSchedulerStaleAgeS", () => {
  it("dates the oldest stale ticker among jobs expected to fire, and stays quiet otherwise (#114309)", () => {
    expect(
      cronSchedulerStaleAgeS([
        { scheduler_heartbeat_age_s: 25 * 3600, enabled: true, state: "scheduled" },
        { scheduler_heartbeat_age_s: 7 * 3600, enabled: true },
        { scheduler_heartbeat_age_s: 90 * 3600, enabled: true, state: "paused" },
      ]),
    ).toBe(25 * 3600);
    expect(cronSchedulerStaleAgeS([{ scheduler_heartbeat_age_s: 90, enabled: true }])).toBeNull();
    expect(cronSchedulerStaleAgeS([{ scheduler_heartbeat_age_s: null, enabled: true }])).toBeNull();
    expect(cronSchedulerStaleAgeS([{ scheduler_heartbeat_age_s: 25 * 3600, enabled: false }])).toBeNull();
    expect(cronSchedulerStaleAgeS([])).toBeNull();
    expect([7 * 3600 + 120, 90, 3 * 86400].map(cronAgoLabel)).toEqual(["7h ago", "1m ago", "3d ago"]);
  });
});
