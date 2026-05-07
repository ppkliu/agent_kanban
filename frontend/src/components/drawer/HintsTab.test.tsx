import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import HintsTab from "./HintsTab";
import type { IssueDetail } from "../../api/types";

vi.mock("../../api/client", () => ({
  api: {
    addHint: vi.fn().mockResolvedValue({ id: 42, consumed: false }),
  },
}));
vi.mock("../../api/ws", () => ({
  dashboardSocket: {
    on: vi.fn(() => () => {}),
    onStatus: vi.fn(() => () => {}),
    connect: vi.fn(),
    close: vi.fn(),
  },
}));

import { api } from "../../api/client";

function makeDetail(overrides: Partial<IssueDetail> = {}): IssueDetail {
  return {
    issue: { id: "id-MT-1", identifier: "MT-1", title: "T", labels: [] },
    current_attempt: null,
    all_attempts: [],
    hints: [],
    rendered_prompt_preview: null,
    workspace_path: null,
    tracker_url: null,
    ...overrides,
  };
}

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
});
afterEach(() => {
  localStorage.clear();
});

describe("HintsTab", () => {
  it("submit button stays disabled when author or content is empty", async () => {
    const user = userEvent.setup();
    render(<HintsTab detail={makeDetail()} onChanged={() => {}} />);
    const button = screen.getByRole("button", { name: /add hint/i });
    expect(button).toBeDisabled();

    await user.type(screen.getByPlaceholderText(/your name/i), "alice");
    expect(button).toBeDisabled();

    await user.type(screen.getByPlaceholderText(/useReducer/i), "use useReducer");
    expect(button).not.toBeDisabled();
  });

  it("submitting calls api.addHint with author + content and persists author to localStorage", async () => {
    const onChanged = vi.fn();
    const user = userEvent.setup();
    render(<HintsTab detail={makeDetail()} onChanged={onChanged} />);
    await user.type(screen.getByPlaceholderText(/your name/i), "alice");
    await user.type(screen.getByPlaceholderText(/useReducer/i), "swap to useReducer");
    await user.click(screen.getByRole("button", { name: /add hint/i }));

    expect(api.addHint).toHaveBeenCalledWith(
      "id-MT-1",
      "alice",
      "swap to useReducer",
    );
    expect(localStorage.getItem("symphony.author")).toBe("alice");
    expect(onChanged).toHaveBeenCalled();
  });

  it("displays pending and consumed hints separately with attempt # annotation", () => {
    const detail = makeDetail({
      hints: [
        {
          id: 1,
          author: "alice",
          content: "first",
          created_at: "2026-05-06T03:14:00Z",
          consumed: false,
          consumed_at: null,
          consumed_attempt: null,
        },
        {
          id: 2,
          author: "bob",
          content: "second",
          created_at: "2026-05-06T03:15:00Z",
          consumed: true,
          consumed_at: "2026-05-06T03:16:00Z",
          consumed_attempt: 2,
        },
      ],
    });
    render(<HintsTab detail={detail} onChanged={() => {}} />);
    expect(
      screen.getByText(/Pending injection \(1\)/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/Consumed history \(1\)/i)).toBeInTheDocument();
    expect(screen.getByText("first")).toBeInTheDocument();
    expect(screen.getByText("second")).toBeInTheDocument();
    expect(screen.getByText(/at attempt #2/i)).toBeInTheDocument();
  });

  it("pre-fills author from localStorage on mount", () => {
    localStorage.setItem("symphony.author", "saved-user");
    render(<HintsTab detail={makeDetail()} onChanged={() => {}} />);
    expect(screen.getByPlaceholderText(/your name/i)).toHaveValue("saved-user");
  });
});
