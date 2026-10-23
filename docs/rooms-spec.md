# Rooms and Prorated Rent (spec)

Source: https://claude.ai/artifact/AkyfxfumhD1VGcPtFcQgMX (22 Oct 2026). People are assigned to rooms for date ranges. Each room carries a fixed percent of the rent, and a rent payment is split by who was in each room on each day of its month. Room assignments replace people.active_from / active_until and the Rent default split.

## Decisions
- Vacant room-days: the rent payment is flagged for review. A person assigns the vacant share (to the House or to people) before it counts.
- Shared rooms: one person per room at a time; a room's assignments can't overlap.
- Who's in the house: for everything except rent, a person counts as in the house for a month if assigned to any room on any day of it. Only rent is prorated.
- History table: editable list per room, newest first, sticky headers. Next to it a read-only timeline with rooms as columns, each cell in the person's color.
- Proration: days assigned / days in that calendar month (28-31).
- Room percent: one per room. Changing it affects rent splits written afterwards; existing splits keep theirs.
- Percent total: room percents must total exactly 100%, checked the same way as split percents (exact decimals, "0.001% is still unassigned").
- Existing data: rooms and assignments seeded from the Rent default split and old active months, then reviewed by the user.
- Person colors: each person gets a random, distinct color, stored with them.

## Data model
Dates are YYYY-MM-DD text. Both ends inclusive (they are nights); blank end = still there.

    CREATE TABLE rooms (
        id       INTEGER PRIMARY KEY,
        name     TEXT    NOT NULL UNIQUE,
        percent  REAL    NOT NULL CHECK (percent > 0)   -- all rooms total exactly 100
    );
    CREATE TABLE room_assignments (
        id          INTEGER PRIMARY KEY,
        room_id     INTEGER NOT NULL REFERENCES rooms (id),
        person_id   INTEGER NOT NULL REFERENCES people (id),
        start_date  TEXT    NOT NULL,                  -- first night in the room
        end_date    TEXT,                              -- last night; NULL = ongoing
        seeded      INTEGER NOT NULL DEFAULT 0,        -- 1 = created by the migration, not yet checked
        CHECK (end_date IS NULL OR end_date >= start_date)
    );
    ALTER TABLE people ADD COLUMN color TEXT;          -- '#rrggbb'
    ALTER TABLE purposes ADD COLUMN split_by_rooms INTEGER NOT NULL DEFAULT 0;  -- set on Rent only
    -- people.active_from and people.active_until are dropped.

Rules the app enforces:
- A room's assignments never overlap. Saving one that does is refused; the message names the clashing person and dates.
- One room per person per day (assumed answer to open question; enforced like overlaps).
- The House can't be assigned to a room.
- Room percents are entered in one editor, saved together, must total exactly 100%.
- Only one purpose has split_by_rooms. Migration sets it on Rent (so renaming Rent doesn't break anything).

## How a rent payment is split
A rent payment covers its transaction month (the "applies to month" override if set, else month of its date). For each room, each day of that month is either a person's or vacant.
1. Each person's weight = sum over rooms they were in that month of room percent x days in that room. Weights are finite decimals, so share calc stays exact (use Decimal).
2. Vacant weight = room percent x vacant days, summed over rooms. Full house -> 0.
3. All weights add up to 100 x days in month. Each person's share = amount x weight / total, using existing share rules (round down to cent; leftover cents one each, rotating).
4. Vacant weight 0 -> written as a default split, same as today.
5. Vacant weight > 0 -> payment gets no split yet and is flagged for review. Nobody else's share is scaled up.

Worked example: July 2026 (31 days), rent $15,728.03. Front 40% Ann 31d (1240) $6,291.21; Back 33.375% Bob 31d (1034.625) $5,249.23; Attic 20% Cy 10d (200) $1,014.71; Attic vacant 11-31 Jul 21d (420); Nook 6.625% vacant 1-14 Jul 14d (92.75); Nook Moana 17d (112.625) $571.41. Total weight 3100. People $13,126.56 + $2,601.47 vacant = $15,728.03.

## Review when a room is vacant
Replaces the current "left out" review for rent. The review flag on other purposes stays and now uses room assignments to decide who's in the house.
- Categorize editor: split shows one row per person with room, days, weight, plus a Vacant row. Its share must go to the House or named people before Save is allowed. Warning names rooms and dates, e.g. "Attic vacant 11-31 Jul (420 of 3,100)."
- Needs review filter: rent payments with an unassigned vacant share appear there.
- Balances: existing warning counts them (their money is in nobody's share yet).
- Rules and imports: a rent payment tagged automatically gets its room split if the month is fully occupied, else flagged. Never split by guesswork.
- Past rent payments: stored splits not changed. Editor offers "Recalculate from rooms" on a rent payment (one at a time) and shows old and new shares side by side before saving.

## Screens
### Rooms (new page): three parts top to bottom
- Room percents: one editor (Room, Percent of rent, Now = current occupant chip), saved together, total row.
- Assignments per room: editable, newest first by first night (Person, First night, Last night, Status). Rows added/edited/deleted in place. Vacant rows computed from gaps, not editable. Seeded rows marked "seeded, check" until edited or confirmed. Streamlit's editable table can't color cells, so color only on person name.
- Timeline: read-only, one row per date where any room changes hands, newest first, columns = rooms, cells colored by person, vacant shown. Header row and From column sticky. st.dataframe + pandas Styler.
- "Not in a room" list: people with old active months but no room (from migration), with their old months.
### People
- Remove Active from / Active until columns and controls, and move-out.
- New columns: color swatch, current room, status (In the house / Moved out / Not yet arrived) from assignments for this month.
- "New color" button picks another random color for that person.
### Elsewhere
- Categorize: person picker marks people who weren't in the house that month (as today). Rent split editor is the room table above.
- Purposes: Rent shows "Split by rooms" instead of a default split editor.
- Balances: "moved out" = no room on or after the selected month and at least one before it.

## Person colors
New people get a color when added. Hues spaced by golden angle (137.5 deg) from a random starting hue. Lightness and chroma fixed so text stays readable on the color in light and dark themes. Stored as hex on the person; migration gives existing people colors the same way.

## Migration from current data
1. Create one room per person in the Rent default split, named "<Person>'s room", with that person's current Rent percent (total exactly 100% today).
2. Assign each to their room. First night: 1st of old Active from month, or if none, 1st of earliest month with transactions. Last night: last day of old Active until month, or ongoing. Every seeded row marked seeded.
3. People with active months not in Rent default split get no room; Rooms page lists them under "Not in a room" with old months. Until placed they don't count as in the house.
4. Set split_by_rooms on Rent and remove Rent's default split.
5. Give everyone a color, then drop active_from and active_until.
6. No stored split changes (including rent payments currently waiting for review).

## Code changes
- housefin/db.py: migration v5 (tables, seed, drop columns). is_active and active_people answer from room assignments.
- housefin/rooms.py: new — rooms, assignments, overlap checks, vacancy gaps, timeline, per-month occupancy (days per person, vacant days per room).
- housefin/splits.py: room split for split_by_rooms purposes in apply_default_split, flagging when vacant.
- housefin/people.py: remove active-month functions; status from assignments; colors.
- housefin/categorize.py: rent editor rows, vacant-share assignment, recalc from rooms.
- pages/: new Rooms page; People, Categorize, Purposes, Balances changes.

## Open questions (assumed answers — use these unless the user says otherwise)
- Two rooms same day? No: one room per person per day.
- Room the House covers? No: each month's vacancy decided at review.
- Days are nights, both inclusive; move-out on the 10th = last night the 9th.
- Recalculating past rent: one at a time from Categorize.
- Utilities for part-month stays: whole month counts. Split still editable by hand.

## Not in this change
Prorating anything but rent; shared rooms; dated history of room percents.
