import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SlateTable } from "@/components/SlateTable";
import { TicketCard } from "@/components/TicketCard";
import { TicketStub } from "@/components/TicketStub";
import { Empty, FlagBadge, StatusChip } from "@/components/ui";
import type { LegView, SlateRow, TicketView } from "@/lib/types";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }) }));
vi.mock("@/lib/actions", () => ({ rebookTicket: vi.fn() }));

const KICKOFF = "2026-11-28T17:00:00+00:00";

function leg(changes: Partial<LegView> = {}): LegView {
  return {
    kind: "TOTALS_LEAN",
    event_id: "sr:match:1",
    home: "Delaware",
    away: "UConn",
    kickoff: KICKOFF,
    started: false,
    mode: "safest",
    team: null,
    direction: "Over",
    projection: 53,
    booked_line: 47.5,
    main_line: 48.5,
    odds: 1.81,
    main_odds: 1.87,
    edge_main: 4.5,
    edge_booked: 5.5,
    flags: [],
    ...changes,
  };
}

function ticket(changes: Partial<TicketView> = {}): TicketView {
  return {
    ticket_id: "T1",
    sport: "NCAAF",
    slate: "2026-11-28",
    run_id: "R1",
    kind: "TOTALS_LEAN",
    number: 1,
    status: "dry_run",
    code: "DRYRUN-ABC123",
    reason: null,
    dry_run: true,
    total_odds: 3.28,
    bookmaker_implied_probability: 0.305,
    implied_note: "Bookmaker-implied: the product of 1/odds for every leg.",
    earliest_kickoff: KICKOFF,
    next_kickoff: KICKOFF,
    started_legs: 0,
    model_disagrees: 0,
    superseded_by: null,
    rebooked_by: null,
    rebook_of: null,
    current: true,
    legs: [leg(), leg({ event_id: "sr:match:2", team: "home", booked_line: 23.5 })],
    ...changes,
  };
}

function row(changes: Partial<SlateRow> = {}): SlateRow {
  return {
    event_id: "sr:match:1",
    game_id: "g1",
    home: "Delaware",
    away: "UConn",
    kickoff: KICKOFF,
    started: false,
    eligible: true,
    skip_reason: null,
    home_tiers: ["High", "High"],
    away_tiers: ["Medium", "Medium"],
    home_l5: { pf: 35, pa: 20, n: 5 },
    away_l5: { pf: 30, pa: 21, n: 5 },
    sum_of_avgs: 65,
    model_total: 53,
    outlook: "MEDIUM",
    main_line: 48.5,
    lean: "OVER",
    edge_main: 4.5,
    booked_line: 47.5,
    booked_odds: 1.81,
    edge_booked: 5.5,
    home_proj: 28,
    away_proj: 25,
    team_pick: { team: "Delaware", side: "home", direction: "Over", line: 23.5, odds: 1.87,
                 projection: 28, edge: 4.5 }, // prettier-ignore
    flags: [],
    same_level_total: 53,
    same_level_lean: "OVER",
    ...changes,
  };
}

describe("the code stub", () => {
  it("copies when tapped and says so", async () => {
    const user = userEvent.setup();
    const write = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    render(<TicketStub code="M8M1KN" status="booked" />);
    await user.click(screen.getByRole("button", { name: /M8M1KN/ }));
    expect(write).toHaveBeenCalledWith("M8M1KN");
    expect(await screen.findByText("Copied")).toBeInTheDocument();
  });

  it("shows an invalid code struck through and refuses to copy it", () => {
    render(<TicketStub code="BADONE" status="invalid" />);
    expect(screen.getByRole("button", { name: /BADONE/ })).toBeDisabled();
    expect(screen.getByText("BADONE")).toHaveClass("line-through");
  });

  it("makes a practice code impossible to mistake for a real one", () => {
    render(<TicketStub code="DRYRUN-ABC123" status="dry_run" />);
    expect(screen.getByText(/Practice code/)).toBeInTheDocument();
  });

  it("explains a ticket with no code", () => {
    render(<TicketStub code={null} status="not_booked" />);
    expect(screen.getByText(/wasn't booked/)).toBeInTheDocument();
  });
});

describe("the ticket card", () => {
  it("shows total odds, the bookmaker-implied chance with its caution, and the kind", () => {
    render(<TicketCard ticket={ticket()} realCodes={false} />);
    expect(screen.getByRole("heading", { name: /Totals lean #1/ })).toBeInTheDocument();
    expect(screen.getByText("3.28")).toBeInTheDocument();
    expect(screen.getByText("30.5%")).toBeInTheDocument();
    expect(screen.getByText(/Bookmaker-implied: the product/)).toBeInTheDocument();
    expect(screen.getByText(/Safest line/)).toBeInTheDocument();
  });

  it("lists its legs by team or total", async () => {
    const user = userEvent.setup();
    render(<TicketCard ticket={ticket()} realCodes={false} />);
    await user.click(screen.getByText("Legs"));
    expect(screen.getByText("Total Over 47.5")).toBeInTheDocument();
    expect(screen.getByText("Delaware Over 23.5")).toBeInTheDocument();
  });

  it("says when the model disagrees with legs at the booked line", () => {
    render(<TicketCard ticket={ticket({ model_disagrees: 2 })} realCodes={false} />);
    expect(screen.getByText(/disagrees with 2 of these legs/)).toBeInTheDocument();
  });

  it("offers a rebook on a current ticket, and not on a superseded one", () => {
    const { unmount } = render(<TicketCard ticket={ticket()} realCodes={false} />);
    expect(screen.getByRole("button", { name: "Rebook" })).toBeInTheDocument();
    unmount();
    render(
      <TicketCard
        ticket={ticket({ current: false, status: "superseded", superseded_by: "R2" })}
        realCodes={false}
      />,
    );
    expect(screen.queryByRole("button", { name: "Rebook" })).not.toBeInTheDocument();
    expect(screen.getByText(/Superseded by a later run/)).toBeInTheDocument();
  });

  it("shows why a ticket wasn't booked", () => {
    render(
      <TicketCard
        ticket={ticket({ status: "failed", code: null, reason: "BlockedError: denied" })}
        realCodes
      />,
    );
    expect(screen.getByText("BlockedError: denied")).toBeInTheDocument();
  });
});

describe("the slate sheet", () => {
  it("has the owner's columns and a lean in words", () => {
    render(<SlateTable rows={[row()]} />);
    for (const name of ["Model total", "Main line", "Lean", "Team pick", "Flags"]) {
      expect(screen.getByRole("columnheader", { name: new RegExp(name) })).toBeInTheDocument();
    }
    expect(screen.getByText("OVER")).toBeInTheDocument();
    expect(screen.getByText("47.5 @ 1.81")).toBeInTheDocument();
    expect(screen.getByText("Delaware Over 23.5 @ 1.87")).toBeInTheDocument();
  });

  it("sorts a number column biggest first, then flips on a second tap", async () => {
    const user = userEvent.setup();
    render(
      <SlateTable
        rows={[row({ event_id: "a", home: "Alpha", model_total: 40 }),
               row({ event_id: "b", home: "Bravo", model_total: 60 })]}
      />, // prettier-ignore
    );
    await user.click(screen.getByRole("button", { name: /Model total/ }));
    const first = () => screen.getAllByRole("row")[1] as HTMLElement;
    expect(within(first()).getByText(/Bravo/)).toBeInTheDocument(); // Biggest first.
    await user.click(screen.getByRole("button", { name: /Model total/ }));
    expect(within(first()).getByText(/Alpha/)).toBeInTheDocument();
  });

  it("greys out a game that has started and shows flags as badges", () => {
    render(<SlateTable rows={[row({ started: true, flags: ["cross_level_game"] })]} />);
    const line = screen.getAllByRole("row")[1] as HTMLElement;
    expect(line).toHaveClass("text-muted");
    expect(within(line).getByText("Cross-level game")).toBeInTheDocument();
  });

  it("marks a game skipped for too few games", () => {
    render(<SlateTable rows={[row({ eligible: false, skip_reason: "min_games", lean: null })]} />);
    expect(screen.getByText("Too few games")).toBeInTheDocument();
    expect(screen.getByText("Skip")).toBeInTheDocument();
  });
});

describe("small parts", () => {
  it("never shows a state by colour alone", () => {
    render(<StatusChip status="invalid" />);
    expect(within(screen.getByText("Invalid")).getByText("✕")).toBeInTheDocument();
  });

  it("gives a flag its words, not just a warning sign", () => {
    render(<FlagBadge label="Cross-level game" />);
    expect(screen.getByText("Cross-level game")).toBeInTheDocument();
  });

  it("gives an empty state its reason", () => {
    render(<Empty title="No tickets">Nothing qualified.</Empty>);
    expect(screen.getByText("Nothing qualified.")).toBeInTheDocument();
  });
});
