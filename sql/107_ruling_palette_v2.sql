-- Ruling palette v2: a palette worth claiming, colours that cannot be
-- mistaken for a map feature, and an automatic claim for every rider who
-- never picked one.
--
-- Three changes, one migration, because they only make sense together:
-- the auto-assigner needs an emoji -> hue mapping, the mapping needs hue
-- families to be a thing the database knows about, and handing every
-- account a colour is only an improvement if the colours are good.
--
-- 1. WHY V1 WASN'T GOOD ENOUGH
-- ----------------------------
-- sql/044 seeded 128 colours as 16 hue families x 8 OKLCH lightness
-- steps, L 0.32..0.86, chroma capped at 0.18. On the map that produced:
--
--   * 48 entries (steps 600-800) that were mud. Below L 0.40 a
--     gamut-fitted chroma of ~0.11 is nearly neutral, so red-800
--     (#5f1113), yellow-800 (#3b3300) and lime-800 (#293900) all render
--     as "dark brown" -- and at the one fill opacity every territory uses
--     (0.55, leaderboard.ts) they smear into the same grey over the
--     basemap.
--   * a chroma ceiling too low to read as identity. The palette's job is
--     to say WHOSE hexagon this is from across the map; a wall of dusty
--     mid-tones cannot.
--   * sixteen families that were really about eleven. red (25 deg) and
--     crimson (5 deg) sit 20 degrees apart, as do cyan and teal, so the
--     picker showed pairs of columns that looked like one column.
--
-- v2 is 14 families x 6 steps, L 0.46..0.85, chroma requested to 0.23 and
-- gamut-fitted down, with every family at least 24 degrees from its
-- neighbours. 84 candidates, 74 survivors (see 2). The method, the
-- numbers and the dropped list are all in scripts/gen_ruling_palette.py,
-- which generated the VALUES below.
--
-- 2. COLOURS THAT COLLIDE WITH THE MAP ARE DROPPED, NOT DIMMED
-- ------------------------------------------------------------
-- A territory does not render alone. The city's micromobility zones
-- (red / orange / yellow), the equity areas (purple), the Rover zone
-- (teal), the ride trail and route (blue / green / orange) and the hex
-- metric outline all draw over or beside it, and most of them MEAN
-- something -- a hexagon outlined in the no-ride zone's red reads as a
-- restriction, not as somebody's claim.
--
-- So the generator measures every candidate against those frontend
-- colours in OKLab and drops anything within 0.055 of one: ten entries,
-- almost exactly one per map feature, which is what a calibrated
-- threshold looks like. Dropping rather than dimming because
-- this has to hold for the BORDER too: borders render fully opaque
-- (leaderboard.ts) against zone outlines that are also fully opaque, so
-- there is no opacity left to hide behind.
--
-- 3. V1'S COLOURS ARE RETIRED, NOT DELETED
-- ----------------------------------------
-- `accounts.ruling_color` references `ruling_colors(hex)`, and riders
-- hold v1 colours today. Deleting the rows would break the FK; silently
-- recolouring those riders would take away a claim they made. So
-- `ruling_colors` grows a `selectable` flag: v1's 128 go FALSE (still
-- valid to hold, no longer offered), v2's 74 go TRUE. The picker shows
-- the selectable ones plus whatever the caller already holds
-- (src/api_lexicon.py), so a v1 holder sees their own colour in the grid
-- instead of a hole where it used to be.
--
-- 4. EVERY RIDER GETS COLOURS, MATCHED TO THEIR EMOJI
-- ---------------------------------------------------
-- An uncoloured leader rendered as a grey ghost (leaderboard.ts's
-- UNCLAIMED_FILL_OPACITY), which is the least interesting thing a held
-- hexagon can look like, and the fix was buried two screens into the
-- profile editor. Now the colours come from the username the rider
-- already has: a 🐸 rules in green, a 🦉 in amber, a 🍇 in purple.
--
-- `emoji_nouns.hue_family` is that mapping, one curated row per emoji
-- (see sql/025 for why curated lists and not inference), and
-- `assign_ruling_colors()` turns it into a free (fill, border) pair. It
-- is a SUGGESTION made concrete, not a lock: the rider can still open the
-- picker and claim anything, and clearing their colours releases the pair
-- exactly as before.
--
-- The assigner never overwrites. It fires for an account whose pair is
-- (NULL, NULL) -- the backfill at the bottom of this file for existing
-- riders, src/accounts.py:assign_public_username for new ones -- and
-- returns FALSE without touching anything otherwise.

-- ---------------------------------------------------------------------
-- Hue families, as a table
-- ---------------------------------------------------------------------
-- v1 kept `hue_family` as a free TEXT column on ruling_colors, which was
-- fine while it only grouped a picker. Two things now read it: the emoji
-- mapping (which must name a family that exists) and the assigner's
-- fallback (which must know that amber is next to orange and nowhere
-- near blue). Both need the WHEEL, so the wheel is stored -- as the OKLCH
-- hue angle the generator used, which is the same number that decides
-- what a colour looks like.
CREATE TABLE IF NOT EXISTS ruling_hue_families (
    family       TEXT PRIMARY KEY,
    hue_degrees  INTEGER NOT NULL
                 CHECK (hue_degrees >= 0 AND hue_degrees < 360),
    -- FALSE for a family v2 no longer generates. Its colours survive for
    -- the riders holding them (see 3 in the header); nothing new lands in
    -- it, and the assigner never falls back into it.
    selectable   BOOLEAN NOT NULL DEFAULT TRUE
);

INSERT INTO ruling_hue_families (family, hue_degrees, selectable) VALUES
    ('red', 27, TRUE),
    ('rose', 1, TRUE),
    ('magenta', 337, TRUE),
    ('purple', 313, TRUE),
    ('violet', 289, TRUE),
    ('indigo', 265, TRUE),
    ('blue', 245, TRUE),
    ('cyan', 215, TRUE),
    ('teal', 191, TRUE),
    ('emerald', 165, TRUE),
    ('green', 141, TRUE),
    ('lime', 117, TRUE),
    ('amber', 85, TRUE),
    ('orange', 55, TRUE),
    -- v1-only families. They exist so the FK below can be added without
    -- rewriting history: sql/044's rows still name them.
    ('crimson', 5, FALSE),
    ('sky', 235, FALSE),
    ('yellow', 100, FALSE)
ON CONFLICT (family) DO UPDATE SET
    hue_degrees = EXCLUDED.hue_degrees,
    selectable = EXCLUDED.selectable;

-- ---------------------------------------------------------------------
-- ruling_colors grows a step and a retirement flag
-- ---------------------------------------------------------------------
-- The DEFAULT on lightness_step is not for new colours -- the generator
-- emits the column, and every row below names it. It is there so sql/044
-- can be REPLAYED against a database that already has this migration
-- applied, which the _pg test fixtures do on every test. 044's INSERT
-- predates the column, and Postgres builds (and NOT NULL-checks) the
-- candidate tuple BEFORE the ON CONFLICT arbiter gets to discard it, so
-- without a default the replay fails on a row it was never going to
-- write. 500 is the middle step, and it is only ever seen by a tuple on
-- its way to being thrown away.
-- Added nullable, then defaulted, backfilled and tightened, so the whole
-- block is a no-op on replay whichever state it finds the column in --
-- ADD COLUMN IF NOT EXISTS skips an existing column INCLUDING its
-- default, so the default cannot ride along on the ADD.
ALTER TABLE ruling_colors
    ADD COLUMN IF NOT EXISTS lightness_step INTEGER,
    ADD COLUMN IF NOT EXISTS selectable     BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE ruling_colors ALTER COLUMN lightness_step SET DEFAULT 500;

-- The step is in every name already ('red-500'); lifting it into a column
-- is what lets the assigner say "a fill, and a border at least two steps
-- darker" without parsing strings in a hot loop. v1's 128 rows predate
-- the column and arrived on the DEFAULT, so read their real step back out
-- of the name they already carry.
UPDATE ruling_colors
   SET lightness_step = split_part(name, '-', 2)::INTEGER
 WHERE lightness_step IS DISTINCT FROM split_part(name, '-', 2)::INTEGER;

ALTER TABLE ruling_colors ALTER COLUMN lightness_step SET NOT NULL;

-- Retire everything, then the INSERT below un-retires exactly v2. Doing
-- it in that order (rather than naming v1's 128 hexes) means a colour
-- that appears in both palettes keeps its claim and stays offered, and
-- there is no list to keep in sync.
UPDATE ruling_colors SET selectable = FALSE WHERE selectable;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ruling_colors_hue_family_fkey'
          AND conrelid = 'ruling_colors'::regclass AND contype = 'f'
    ) THEN
        ALTER TABLE ruling_colors
            ADD CONSTRAINT ruling_colors_hue_family_fkey
            FOREIGN KEY (hue_family) REFERENCES ruling_hue_families(family);
    END IF;
END $$;

-- v2's sort_order restarts at 0 and overlaps v1's. That is deliberate and
-- harmless: sort_order orders a picker that only ever shows one palette's
-- worth of colours at a time (the selectable ones), and the index below
-- keeps the retired half from interleaving into it.
CREATE INDEX IF NOT EXISTS ruling_colors_selectable_idx
    ON ruling_colors (sort_order) WHERE selectable;


-- ---------------------------------------------------------------------
-- emoji_nouns -> hue family
-- ---------------------------------------------------------------------
-- One row per emoji in sql/025's list, by what the glyph actually looks
-- like: 🐸 green, 🦉 amber, 🍇 purple, 🦩 rose. Hand-written for the same
-- reason the noun list is hand-written -- there is no rule that gets
-- 🧩 right -- and NULLable so a future emoji can land before anyone has
-- decided what colour it is (assign_ruling_colors falls back to a stable
-- hash of the emoji, so an unmapped one still gets a consistent colour
-- rather than no colour).
--
-- The distribution is lopsided on purpose: 51 of 181 nouns are amber,
-- because a great many animals and foods are brown or yellow. The
-- assigner's family fallback (below) is what absorbs that, not a
-- flattened mapping that would put 🐶 in cyan to even out a histogram.
ALTER TABLE emoji_nouns
    ADD COLUMN IF NOT EXISTS hue_family TEXT REFERENCES ruling_hue_families(family);

UPDATE emoji_nouns AS n
   SET hue_family = v.family
  FROM (VALUES
    ('🐶', 'amber'),
    ('🐱', 'amber'),
    ('🦊', 'orange'),
    ('🐼', 'indigo'),
    ('🦁', 'amber'),
    ('🐯', 'orange'),
    ('🐨', 'violet'),
    ('🐻', 'amber'),
    ('🐷', 'rose'),
    ('🐮', 'rose'),
    ('🐴', 'amber'),
    ('🦄', 'magenta'),
    ('🦓', 'indigo'),
    ('🦌', 'amber'),
    ('🐘', 'violet'),
    ('🦒', 'amber'),
    ('🦛', 'purple'),
    ('🦏', 'violet'),
    ('🐵', 'amber'),
    ('🦍', 'indigo'),
    ('🦘', 'amber'),
    ('🐑', 'lime'),
    ('🐐', 'amber'),
    ('🐫', 'amber'),
    ('🦙', 'amber'),
    ('🐰', 'rose'),
    ('🦔', 'amber'),
    ('🦇', 'purple'),
    ('🐀', 'violet'),
    ('🐭', 'violet'),
    ('🐹', 'amber'),
    ('🐺', 'indigo'),
    ('🦦', 'amber'),
    ('🦨', 'indigo'),
    ('🦡', 'indigo'),
    ('🦥', 'lime'),
    ('🦝', 'violet'),
    ('🐿️', 'orange'),
    ('🦬', 'amber'),
    ('🐔', 'red'),
    ('🐓', 'red'),
    ('🐤', 'amber'),
    ('🐧', 'indigo'),
    ('🦅', 'amber'),
    ('🦆', 'amber'),
    ('🦉', 'amber'),
    ('🦩', 'rose'),
    ('🦚', 'teal'),
    ('🦜', 'green'),
    ('🦢', 'cyan'),
    ('🦃', 'red'),
    ('🦤', 'lime'),
    ('🐳', 'blue'),
    ('🐬', 'cyan'),
    ('🦈', 'blue'),
    ('🐟', 'cyan'),
    ('🐠', 'orange'),
    ('🐡', 'amber'),
    ('🐙', 'magenta'),
    ('🦑', 'rose'),
    ('🦐', 'orange'),
    ('🦞', 'red'),
    ('🦀', 'red'),
    ('🦭', 'indigo'),
    ('🐢', 'green'),
    ('🐌', 'amber'),
    ('🐸', 'green'),
    ('🐝', 'amber'),
    ('🦋', 'violet'),
    ('🐞', 'red'),
    ('🐜', 'indigo'),
    ('🕷️', 'indigo'),
    ('🦗', 'emerald'),
    ('🪲', 'teal'),
    ('🪱', 'rose'),
    ('🦟', 'violet'),
    ('🐉', 'green'),
    ('🦕', 'green'),
    ('🦖', 'emerald'),
    ('🌲', 'green'),
    ('🌴', 'emerald'),
    ('🌵', 'green'),
    ('🌱', 'lime'),
    ('🌿', 'emerald'),
    ('🍀', 'green'),
    ('🌻', 'amber'),
    ('🌷', 'rose'),
    ('🌹', 'red'),
    ('🌺', 'magenta'),
    ('🌸', 'rose'),
    ('🍄', 'red'),
    ('🌰', 'amber'),
    ('🍁', 'orange'),
    ('🍃', 'lime'),
    ('🌙', 'amber'),
    ('🌈', 'magenta'),
    ('🚀', 'indigo'),
    ('🪐', 'amber'),
    ('⭐', 'amber'),
    ('🍎', 'red'),
    ('🍐', 'lime'),
    ('🍊', 'orange'),
    ('🍋', 'amber'),
    ('🍉', 'red'),
    ('🍇', 'purple'),
    ('🍓', 'red'),
    ('🍒', 'red'),
    ('🍍', 'amber'),
    ('🥭', 'orange'),
    ('🥥', 'amber'),
    ('🥝', 'lime'),
    ('🍅', 'red'),
    ('🥕', 'orange'),
    ('🌽', 'amber'),
    ('🥔', 'amber'),
    ('🍞', 'amber'),
    ('🥐', 'amber'),
    ('🥨', 'orange'),
    ('🧀', 'amber'),
    ('🥞', 'amber'),
    ('🍯', 'amber'),
    ('🍪', 'orange'),
    ('🧁', 'rose'),
    ('🍩', 'rose'),
    ('🍦', 'rose'),
    ('🍕', 'orange'),
    ('🌮', 'orange'),
    ('🍿', 'amber'),
    ('🍬', 'rose'),
    ('🍭', 'magenta'),
    ('🍫', 'orange'),
    ('🎈', 'red'),
    ('🎁', 'red'),
    ('🪁', 'cyan'),
    ('⚓', 'indigo'),
    ('🧭', 'indigo'),
    ('🔑', 'amber'),
    ('🏮', 'red'),
    ('💡', 'amber'),
    ('🔭', 'indigo'),
    ('🧲', 'red'),
    ('💎', 'cyan'),
    ('👑', 'amber'),
    ('🏆', 'amber'),
    ('🏅', 'amber'),
    ('🥁', 'red'),
    ('🎸', 'orange'),
    ('🎺', 'amber'),
    ('🎻', 'orange'),
    ('🪕', 'amber'),
    ('🎨', 'magenta'),
    ('🎭', 'purple'),
    ('🎪', 'red'),
    ('🎡', 'magenta'),
    ('🎢', 'blue'),
    ('🧩', 'orange'),
    ('🪀', 'red'),
    ('🪃', 'orange'),
    ('🎯', 'red'),
    ('🧸', 'orange'),
    ('🪆', 'red'),
    ('🕯️', 'amber'),
    ('📚', 'emerald'),
    ('📖', 'blue'),
    ('✏️', 'amber'),
    ('🖊️', 'indigo'),
    ('🔔', 'amber'),
    ('🎵', 'indigo'),
    ('🎶', 'indigo'),
    ('🌊', 'blue'),
    ('🏔️', 'indigo'),
    ('🏝️', 'teal'),
    ('🗻', 'indigo'),
    ('🌋', 'red'),
    ('🧊', 'cyan'),
    ('🔥', 'orange'),
    ('💧', 'cyan'),
    ('🍂', 'orange'),
    ('🐦', 'blue'),
    ('🐕', 'amber'),
    ('🐈', 'amber')
  ) AS v(emoji, family)
 WHERE n.emoji = v.emoji
   AND n.hue_family IS DISTINCT FROM v.family;

-- ---------------------------------------------------------------------
-- The v2 palette
-- ---------------------------------------------------------------------
-- 74 colours: 14 hue families x 6 steps, minus 10 that read as a map feature.
-- Generated by scripts/gen_ruling_palette.py — see that file for the method.
-- dropped red-400 (#d90a1a): 0.049 from #c1121f no-ride zone (micromobility-zones.ts)
-- dropped purple-300 (#7b299f): 0.043 from #6a1b9a equity area (equity-areas.ts)
-- dropped indigo-400 (#3164f6): 0.023 from #0066ff ride trail (ride-trail.ts)
-- dropped blue-400 (#007ac1): 0.028 from #2171b5 hex-metric ramp outline (hexdensity.ts)
-- dropped teal-400 (#008682): 0.017 from #00897b Rover zone (rover-zone.ts)
-- dropped emerald-400 (#008a63): 0.038 from #00897b Rover zone (rover-zone.ts)
-- dropped green-400 (#1e8d00): 0.043 from #238636 trail start point (ride-trail.ts)
-- dropped amber-700 (#e8b121): 0.009 from #eab308 slow zone (micromobility-zones.ts)
-- dropped orange-500 (#d36d00): 0.032 from #d55e00 route destination (ride-route-line.ts)
-- dropped orange-600 (#f68000): 0.043 from #e07a00 no-parking / slow-no-parking zone (micromobility-zones.ts)
INSERT INTO ruling_colors
    (hex, name, hue_family, lightness_step, sort_order, selectable) VALUES
    ('#a70511', 'red-300', 'red', 300, 0, TRUE),
    ('#f93534', 'red-500', 'red', 500, 1, TRUE),
    ('#ff7266', 'red-600', 'red', 600, 2, TRUE),
    ('#ff9b90', 'red-700', 'red', 700, 3, TRUE),
    ('#ffbab1', 'red-800', 'red', 800, 4, TRUE),
    ('#a10252', 'rose-300', 'rose', 300, 5, TRUE),
    ('#d2046d', 'rose-400', 'rose', 400, 6, TRUE),
    ('#f13284', 'rose-500', 'rose', 500, 7, TRUE),
    ('#ff689d', 'rose-600', 'rose', 600, 8, TRUE),
    ('#ff95b5', 'rose-700', 'rose', 700, 9, TRUE),
    ('#ffb6ca', 'rose-800', 'rose', 800, 10, TRUE),
    ('#92187c', 'magenta-300', 'magenta', 300, 11, TRUE),
    ('#bf23a3', 'magenta-400', 'magenta', 400, 12, TRUE),
    ('#dc40be', 'magenta-500', 'magenta', 500, 13, TRUE),
    ('#ee6dd0', 'magenta-600', 'magenta', 600, 14, TRUE),
    ('#f891df', 'magenta-700', 'magenta', 700, 15, TRUE),
    ('#feb1e9', 'magenta-800', 'magenta', 800, 16, TRUE),
    ('#a139cf', 'purple-400', 'purple', 400, 17, TRUE),
    ('#bb53ed', 'purple-500', 'purple', 500, 18, TRUE),
    ('#ce7afa', 'purple-600', 'purple', 600, 19, TRUE),
    ('#dc9cff', 'purple-700', 'purple', 700, 20, TRUE),
    ('#e6baff', 'purple-800', 'purple', 800, 21, TRUE),
    ('#5a3ab6', 'violet-300', 'violet', 300, 22, TRUE),
    ('#774eec', 'violet-400', 'violet', 400, 23, TRUE),
    ('#8d6eff', 'violet-500', 'violet', 500, 24, TRUE),
    ('#a392ff', 'violet-600', 'violet', 600, 25, TRUE),
    ('#b8aeff', 'violet-700', 'violet', 700, 26, TRUE),
    ('#cbc6ff', 'violet-800', 'violet', 800, 27, TRUE),
    ('#234bbd', 'indigo-300', 'indigo', 300, 28, TRUE),
    ('#5183ff', 'indigo-500', 'indigo', 500, 29, TRUE),
    ('#78a1ff', 'indigo-600', 'indigo', 600, 30, TRUE),
    ('#99baff', 'indigo-700', 'indigo', 700, 31, TRUE),
    ('#b6ceff', 'indigo-800', 'indigo', 800, 32, TRUE),
    ('#005c94', 'blue-300', 'blue', 300, 33, TRUE),
    ('#0093e6', 'blue-500', 'blue', 500, 34, TRUE),
    ('#3bacff', 'blue-600', 'blue', 600, 35, TRUE),
    ('#79c2ff', 'blue-700', 'blue', 700, 36, TRUE),
    ('#a2d4ff', 'blue-800', 'blue', 800, 37, TRUE),
    ('#006374', 'cyan-300', 'cyan', 300, 38, TRUE),
    ('#008399', 'cyan-400', 'cyan', 400, 39, TRUE),
    ('#009db7', 'cyan-500', 'cyan', 500, 40, TRUE),
    ('#00b8d6', 'cyan-600', 'cyan', 600, 41, TRUE),
    ('#00d0f2', 'cyan-700', 'cyan', 700, 42, TRUE),
    ('#67e1fd', 'cyan-800', 'cyan', 800, 43, TRUE),
    ('#006663', 'teal-300', 'teal', 300, 44, TRUE),
    ('#00a19c', 'teal-500', 'teal', 500, 45, TRUE),
    ('#00bdb7', 'teal-600', 'teal', 600, 46, TRUE),
    ('#00d6cf', 'teal-700', 'teal', 700, 47, TRUE),
    ('#63e6e0', 'teal-800', 'teal', 800, 48, TRUE),
    ('#00694a', 'emerald-300', 'emerald', 300, 49, TRUE),
    ('#00a577', 'emerald-500', 'emerald', 500, 50, TRUE),
    ('#00c28d', 'emerald-600', 'emerald', 600, 51, TRUE),
    ('#31d9a1', 'emerald-700', 'emerald', 700, 52, TRUE),
    ('#7fe6bb', 'emerald-800', 'emerald', 800, 53, TRUE),
    ('#156b00', 'green-300', 'green', 300, 54, TRUE),
    ('#27a900', 'green-500', 'green', 500, 55, TRUE),
    ('#52c140', 'green-600', 'green', 600, 56, TRUE),
    ('#7fd372', 'green-700', 'green', 700, 57, TRUE),
    ('#a4e19a', 'green-800', 'green', 800, 58, TRUE),
    ('#545f00', 'lime-300', 'lime', 300, 59, TRUE),
    ('#707d00', 'lime-400', 'lime', 400, 60, TRUE),
    ('#879700', 'lime-500', 'lime', 500, 61, TRUE),
    ('#9eb100', 'lime-600', 'lime', 600, 62, TRUE),
    ('#b4c743', 'lime-700', 'lime', 700, 63, TRUE),
    ('#c8d87f', 'lime-800', 'lime', 800, 64, TRUE),
    ('#705300', 'amber-300', 'amber', 300, 65, TRUE),
    ('#936e00', 'amber-400', 'amber', 400, 66, TRUE),
    ('#b08400', 'amber-500', 'amber', 500, 67, TRUE),
    ('#cf9c00', 'amber-600', 'amber', 600, 68, TRUE),
    ('#f0c871', 'amber-800', 'amber', 800, 69, TRUE),
    ('#874300', 'orange-300', 'orange', 300, 70, TRUE),
    ('#b05a00', 'orange-400', 'orange', 400, 71, TRUE),
    ('#ffa05c', 'orange-700', 'orange', 700, 72, TRUE),
    ('#ffbe91', 'orange-800', 'orange', 800, 73, TRUE)
ON CONFLICT (hex) DO UPDATE SET
    name = EXCLUDED.name,
    hue_family = EXCLUDED.hue_family,
    lightness_step = EXCLUDED.lightness_step,
    sort_order = EXCLUDED.sort_order,
    selectable = TRUE;

-- ---------------------------------------------------------------------
-- The assigner
-- ---------------------------------------------------------------------
-- Give one account a free (fill, border) pair matched to its username
-- emoji. Returns TRUE if it set one, FALSE if there was nothing to do
-- (no emoji yet, colours already claimed) or nothing free to give.
--
-- WHAT "MATCHED" MEANS
--   fill   -- the emoji's hue family, one of the four lighter steps. The
--             fill is what a rider sees from across the map at 55%
--             opacity, so it is the half that has to carry the hue.
--   border -- at least two lightness steps darker than the fill,
--             preferring the same family and then the nearest one. A
--             deeper shade of the fill reads as one considered choice;
--             a random contrasting hue reads as a bug.
--
-- WHY IT WIDENS RATHER THAN FAILS
-- Families are not evenly populated (51 of 181 emoji are amber) and each
-- holds ~5 colours. Same-family pairs alone would run out after twenty
-- amber riders. So the candidate set is every selectable fill, ORDERED
-- by how far its family sits from the emoji's around the hue wheel: an
-- amber rider gets amber until amber is gone, then orange, then lime --
-- never blue while a warm colour is free. 1 542 pairs satisfy the
-- fill/border rule (the hand picker's 74 x 73 = 5 402 is the wider
-- figure, since a rider choosing by hand is not held to it), and
-- returning FALSE once they are gone is correct: an uncoloured account
-- still renders (leaderboard.ts draws it as a ghost) and the rider can
-- still pick by hand.
--
-- The deterministic hash in the ORDER BY is what keeps two riders with
-- the same emoji from queueing for the same pair in the same order --
-- without it every 🦉 would try amber-500/amber-300 first and the second
-- one would walk the whole list.
--
-- CONCURRENCY. accounts_ruling_pair_key (sql/044) makes a lost race a
-- UNIQUE violation, which would abort the caller's whole transaction --
-- including, for a brand-new account, the INSERT that created it (see
-- src/accounts.py:assign_public_username for the same reasoning about
-- the same hazard). So: filter out taken pairs in the query, then take
-- the pair's advisory lock and re-check before writing. The lock is the
-- same shape accounts.py uses for usernames, keyed on the pair.
CREATE OR REPLACE FUNCTION assign_ruling_colors(p_account_id BIGINT)
RETURNS BOOLEAN
LANGUAGE plpgsql
AS $fn$
DECLARE
    v_emoji  TEXT;
    v_hue    INTEGER;
    v_pair   RECORD;
BEGIN
    -- FOR UPDATE so two callers for the same account serialize here
    -- rather than both deciding it needs colours.
    SELECT username_emoji INTO v_emoji
      FROM accounts
     WHERE id = p_account_id
       AND ruling_color IS NULL
       AND ruling_border_color IS NULL
       FOR UPDATE;
    IF NOT FOUND OR v_emoji IS NULL THEN
        RETURN FALSE;
    END IF;

    SELECT f.hue_degrees INTO v_hue
      FROM emoji_nouns n
      JOIN ruling_hue_families f ON f.family = n.hue_family
     WHERE n.emoji = v_emoji;

    -- An emoji nobody has assigned a family to yet: pick an angle from a
    -- stable hash of the emoji itself. Still deterministic, still gives
    -- every 🫧 the same hue -- just not a chosen one.
    IF v_hue IS NULL THEN
        v_hue := abs(hashtextextended(v_emoji, 0) % 360);
    END IF;

    FOR v_pair IN
        SELECT fill.hex AS fill, brd.hex AS border
          FROM ruling_colors fill
          JOIN ruling_hue_families fam ON fam.family = fill.hue_family
          JOIN ruling_colors brd
            ON brd.selectable
           AND brd.lightness_step <= fill.lightness_step - 200
          JOIN ruling_hue_families bfam ON bfam.family = brd.hue_family
         WHERE fill.selectable
           AND fill.lightness_step >= 500
           AND NOT EXISTS (
               SELECT 1 FROM accounts a
                WHERE a.ruling_color = fill.hex
                  AND a.ruling_border_color = brd.hex
           )
         ORDER BY
           -- Angular distance around the wheel, so 350 deg and 10 deg are
           -- neighbours rather than opposites.
           LEAST(abs(fam.hue_degrees - v_hue), 360 - abs(fam.hue_degrees - v_hue)),
           -- Then how far the BORDER's family sits from the fill's, which
           -- is 0 for the deeper-shade-of-the-same-hue case and grows from
           -- there. A family can run dry of same-family pairs quickly
           -- (orange keeps four colours after the conflict filter, so four
           -- pairs); when it does, the next-best border is the neighbouring
           -- hue, not whatever the hash happens to surface.
           LEAST(abs(bfam.hue_degrees - fam.hue_degrees),
                 360 - abs(bfam.hue_degrees - fam.hue_degrees)),
           hashtextextended(p_account_id::TEXT || fill.hex || brd.hex, 0)
         LIMIT 16
    LOOP
        PERFORM pg_advisory_xact_lock(
            hashtextextended('ruling_pair:' || v_pair.fill || '|' || v_pair.border, 0)
        );
        IF NOT EXISTS (
            SELECT 1 FROM accounts a
             WHERE a.ruling_color = v_pair.fill
               AND a.ruling_border_color = v_pair.border
        ) THEN
            UPDATE accounts
               SET ruling_color = v_pair.fill,
                   ruling_border_color = v_pair.border
             WHERE id = p_account_id;
            RETURN TRUE;
        END IF;
    END LOOP;

    RETURN FALSE;
END;
$fn$;

-- ---------------------------------------------------------------------
-- Backfill
-- ---------------------------------------------------------------------
-- Every rider who has a username but never picked colours. Ordered by id
-- so a replay of this directory (the _pg fixtures do one per test) builds
-- the same database twice.
--
-- In one statement rather than the usual per-row CLI backfill because
-- this runs inside the migration's transaction anyway -- there is no
-- commit to interleave -- and the whole point is that nobody is left
-- grey the moment this deploys.
-- `python -m src.cli backfill_ruling_colors` exists for the accounts
-- created between this migration and the code that calls the assigner.
DO $$
DECLARE
    v_id      BIGINT;
    v_count   INTEGER := 0;
BEGIN
    FOR v_id IN
        SELECT id FROM accounts
         WHERE ruling_color IS NULL
           AND ruling_border_color IS NULL
           AND username_emoji IS NOT NULL
         ORDER BY id
    LOOP
        IF assign_ruling_colors(v_id) THEN
            v_count := v_count + 1;
        END IF;
    END LOOP;
    RAISE NOTICE 'sql/107: assigned ruling colours to % account(s)', v_count;
END $$;
