import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ChartFrame } from "@/components/charts/ChartFrame";
import { TicketCardView } from "@/components/TicketCardView";
import { WeakLegs } from "@/components/TicketCardView";
import { TicketStub } from "@/components/TicketStub";
import { Empty, ShotClock, StatusChip } from "@/components/ui";
import type { TicketCard } from "@/lib/types";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

function ticket(changes: Partial<TicketCard> = {}): TicketCard {
  return {
    ticket_id: "t1",
    league: "NCAA",
    family: "total_ft",
    ticket_no: 1,
    version: 3,
    status: "verified",
    settle_status: "pending",
    booking_code: "M8M1KN",
    reason: null,
    legs: 1,
    combined_odds: 3.5,
    combined_prob: 0.3,
    expected_value: 0.05,
    passed_gate: true,
    edge_label: "measured",
    probability_note: null,
    correction: false,
    weak_legs: 0,
    legs_summary: [
      {
        rank: 1,
        game_id: "g1",
        matchup: "UNC at Duke",
        pick: "Over 145.5",
        odds: 1.9,
        p_calibrated: 0.56,
        fair: 0.5,
        edge: 0.06,
        tip_time: "2026-11-07T01:00:00+00:00",
      },
    ],
    ...changes,
  };
}

describe("the code stub", () => {
  it("copies when tapped and says so", async () => {
    const user = userEvent.setup();
    const write = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    render(<TicketStub code="M8M1KN" status="verified" />);
    await user.click(screen.getByRole("button", { name: /M8M1KN/ }));
    expect(write).toHaveBeenCalledWith("M8M1KN");
    expect(await screen.findByText("Copied")).toBeInTheDocument();
  });

  it("shows an invalid code struck through and refuses to copy it", () => {
    render(<TicketStub code="BADONE" status="invalid" />);
    const button = screen.getByRole("button", { name: /BADONE/ });
    expect(button).toBeDisabled();
    expect(screen.getByText("BADONE")).toHaveClass("line-through");
    expect(screen.getByText("Don't use this code")).toBeInTheDocument();
  });

  it("makes a practice code impossible to mistake for a real one", () => {
    render(<TicketStub code="DRYRUN-ABC123" status="dry_run" />);
    expect(screen.getByText(/Practice code/)).toBeInTheDocument();
  });

  it("says how many legs are below the tested minimum", () => {
    render(<WeakLegs weak={3} legs={5} />);
    expect(screen.getByText(/3 of 5 legs are below/)).toBeInTheDocument();
  });

  it("says nothing when every leg met the tested minimum", () => {
    const { container } = render(<WeakLegs weak={0} legs={5} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("doesn't warn on a real code", () => {
    render(<TicketStub code="M8M1KN" status="verified" />);
    expect(screen.queryByText(/Practice code/)).not.toBeInTheDocument();
  });

  it("explains a ticket with no code", () => {
    render(<TicketStub code={null} status="failed" />);
    expect(screen.getByText(/wasn't booked/)).toBeInTheDocument();
  });
});

describe("the ticket card", () => {
  it("shows a measured ticket's chance and expected value", () => {
    render(<TicketCardView ticket={ticket()} />);
    expect(screen.getByText("~1 in 3")).toBeInTheDocument();
    expect(screen.getByText("+5%")).toBeInTheDocument();
    expect(screen.getByText("3.50")).toBeInTheDocument();
    expect(screen.queryByText(/failed its evidence gate/)).not.toBeInTheDocument();
  });

  it("shows no probability or EV on an unproven ticket (D72)", async () => {
    const user = userEvent.setup();
    const note = "Unproven: treat each leg as about 50%.";
    const { container } = render(
      <TicketCardView
        ticket={ticket({
          edge_label: "unproven",
          combined_prob: null,
          expected_value: null,
          probability_note: note,
          legs_summary: [
            {
              rank: 1,
              game_id: "g1",
              matchup: "UNC at Duke",
              pick: "1H Over 66.5",
              odds: 1.85,
              p_calibrated: null,
              fair: 0.5,
              edge: null,
              tip_time: "2026-11-07T01:00:00+00:00",
            },
          ],
        })}
      />,
    );
    expect(screen.getByText("Unproven")).toBeInTheDocument();
    expect(screen.getByText(note)).toBeInTheDocument();
    expect(screen.queryByText("EV")).not.toBeInTheDocument();
    expect(container.textContent).not.toMatch(/1 in/);
    await user.click(screen.getByRole("button", { name: "Show 1 legs" }));
    expect(screen.getByText("Implied 50.0%")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/Model/);
    expect(container.textContent).not.toMatch(/Edge/);
  });

  it("flags a market in force that failed its gate (D65)", () => {
    render(<TicketCardView ticket={ticket({ passed_gate: false })} />);
    expect(screen.getByText(/failed its evidence gate/)).toBeInTheDocument();
  });

  it("opens to the legs, with the model against the implied price", async () => {
    const user = userEvent.setup();
    render(<TicketCardView ticket={ticket()} />);
    expect(screen.queryByText("Over 145.5")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show 1 legs" }));
    expect(screen.getByText("Over 145.5")).toBeInTheDocument();
    expect(screen.getByText(/Model 56.0% · Implied 50.0% · Edge \+6.0%/)).toBeInTheDocument();
    expect(screen.getByText("Sat 02:00")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "UNC at Duke" })).toHaveAttribute("href", "/games/g1");
  });

  it("shows why a ticket failed", () => {
    render(
      <TicketCardView ticket={ticket({ status: "failed", reason: "Booking failed: 19000" })} />,
    );
    expect(screen.getByText("Booking failed: 19000")).toBeInTheDocument();
  });
});

describe("small parts", () => {
  it("never shows a state by colour alone", () => {
    render(<StatusChip status="invalid" />);
    const chip = screen.getByText("Invalid");
    expect(within(chip).getByText("✕")).toBeInTheDocument();
  });

  it("gives an empty state its reason", () => {
    render(<Empty title="No tickets">Nothing qualified.</Empty>);
    expect(screen.getByText("Nothing qualified.")).toBeInTheDocument();
  });

  it("lights one shot-clock segment per 1/24 of the chance", () => {
    const { container } = render(<ShotClock value={0.5} fair={0.5} />);
    expect(container.querySelectorAll(".bg-amber")).toHaveLength(12);
    expect(container.querySelectorAll(".outline-text")).toHaveLength(1);
  });

  it("gives every chart a summary and a table with every value", async () => {
    const user = userEvent.setup();
    render(
      <ChartFrame
        title="Hit rate"
        summary="Most legs won."
        legend={[
          { label: "One", color: "red" },
          { label: "Two", color: "blue" },
        ]}
        columns={["Bucket", "Rate"]}
        rows={[["60%", "55.0%"]]}
      >
        <div>chart</div>
      </ChartFrame>,
    );
    expect(screen.getByText("Most legs won.")).toBeInTheDocument();
    expect(screen.getByText("One")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Table" }));
    expect(screen.getByRole("cell", { name: "55.0%" })).toBeInTheDocument();
    expect(screen.queryByText("chart")).not.toBeInTheDocument();
  });

  it("hides the legend for a single series, which the title already names", () => {
    render(
      <ChartFrame
        title="Hit rate"
        summary="s"
        legend={[{ label: "Only", color: "red" }]}
        columns={[]}
        rows={[]}
      >
        <div />
      </ChartFrame>,
    );
    expect(screen.queryByText("Only")).not.toBeInTheDocument();
  });
});
