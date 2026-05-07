import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Notice from "./Notice";
import { useStore } from "../store";

beforeEach(() => {
  useStore.setState({ notice: null });
});
afterEach(() => {
  // Each test sets its own timer mode; reset to real for isolation.
  vi.useRealTimers();
});

describe("Notice", () => {
  it("renders nothing when notice is null", () => {
    const { container } = render(<Notice />);
    expect(container.firstChild).toBeNull();
  });

  it("renders the info notice text", () => {
    useStore.setState({ notice: { kind: "info", text: "Saved" } });
    render(<Notice />);
    expect(screen.getByText("Saved")).toBeInTheDocument();
  });

  it("auto-dismisses an info notice after 3500ms", () => {
    vi.useFakeTimers();
    useStore.setState({ notice: { kind: "info", text: "Bye" } });
    render(<Notice />);
    expect(screen.getByText("Bye")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(3499);
    });
    expect(screen.queryByText("Bye")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(2);
    });
    expect(useStore.getState().notice).toBeNull();
  });

  it("auto-dismisses an error notice after 8000ms (longer)", () => {
    vi.useFakeTimers();
    useStore.setState({ notice: { kind: "error", text: "boom" } });
    render(<Notice />);
    act(() => {
      vi.advanceTimersByTime(3500);
    });
    expect(useStore.getState().notice).not.toBeNull();
    act(() => {
      vi.advanceTimersByTime(4500);
    });
    expect(useStore.getState().notice).toBeNull();
  });

  it("close button clears the notice immediately", async () => {
    const user = userEvent.setup();
    useStore.setState({ notice: { kind: "info", text: "click-to-close" } });
    render(<Notice />);
    await user.click(screen.getByText("×"));
    expect(useStore.getState().notice).toBeNull();
  });
});
