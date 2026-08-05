-- Public-facing profile expansion: SFW adjective + noun-emoji reference
-- tables (back the public_username feature), plus new self-service
-- profile columns on accounts.
--
-- public_username is a GENERATED column, not a plain TEXT column: it is
-- always exactly username_adjective || username_emoji, computed by
-- Postgres itself, so the display string can never drift out of sync
-- with its two constituent parts. (sql/060 later restyled that formula
-- to a capitalized adjective, a space, then the emoji — "Brave 🦉".
-- Everything below is unchanged by it; only the presentation moved.)
-- The parts are stored separately (not
-- just concatenated in Python) specifically so a rider can change just
-- one half later (PUT /api/v1/profile/username) without needing to parse
-- a combined string back apart.
--
-- Riders may choose their own adjective/emoji (validated against the two
-- tables below) or have one randomly assigned — see
-- src/accounts.py:generate_public_username / assign_public_username /
-- choose_public_username. New rows get one immediately at account
-- creation (upsert_account); existing rows are backfilled by
-- `python -m src.cli backfill_public_usernames`, so NULL here only ever
-- describes a pre-migration account that hasn't been backfilled yet.
--
-- email becomes nullable + the CHECK below requires at least one of
-- email/phone_number. sql/026 does a defensive duplicate-email cleanup
-- this enables running safely.

CREATE TABLE IF NOT EXISTS sfw_adjectives (
    word  TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS emoji_nouns (
    emoji  TEXT PRIMARY KEY,
    word   TEXT NOT NULL
);

-- 287 common, single-word, SFW English adjectives. ON CONFLICT DO NOTHING
-- makes re-running (or extending the list later) safe.
INSERT INTO sfw_adjectives (word) VALUES
    ('able'), ('active'), ('adaptable'), ('adorable'), ('affable'), ('agile'), ('alert'), ('amazing'),
    ('ambitious'), ('amiable'), ('ample'), ('amused'), ('apt'), ('artistic'), ('astute'), ('avid'),
    ('awesome'), ('balanced'), ('beaming'), ('blissful'), ('blithe'), ('bold'), ('bouncy'), ('brave'),
    ('breezy'), ('bright'), ('bubbly'), ('bustling'), ('calm'), ('candid'), ('capable'), ('carefree'),
    ('charming'), ('chatty'), ('cheerful'), ('chic'), ('chill'), ('chirpy'), ('chunky'), ('classy'),
    ('clean'), ('clever'), ('cool'), ('cordial'), ('courageous'), ('cozy'), ('crafty'), ('creative'),
    ('crisp'), ('cuddly'), ('curious'), ('dapper'), ('daring'), ('dashing'), ('dazzling'), ('decisive'),
    ('deft'), ('delightful'), ('dependable'), ('determined'), ('devoted'), ('diligent'), ('dreamy'), ('driven'),
    ('dutiful'), ('dynamic'), ('eager'), ('earnest'), ('easygoing'), ('elegant'), ('eloquent'), ('energetic'),
    ('enthusiastic'), ('exuberant'), ('fair'), ('faithful'), ('famous'), ('fancy'), ('fearless'), ('feisty'),
    ('fierce'), ('fine'), ('flashy'), ('fluffy'), ('fond'), ('frank'), ('free'), ('fresh'),
    ('friendly'), ('frisky'), ('fun'), ('funky'), ('gallant'), ('generous'), ('genial'), ('gentle'),
    ('glad'), ('gleeful'), ('glowing'), ('golden'), ('good'), ('graceful'), ('gracious'), ('grand'),
    ('grateful'), ('groovy'), ('handy'), ('happy'), ('hardy'), ('harmonious'), ('hearty'), ('helpful'),
    ('heroic'), ('hip'), ('honest'), ('hopeful'), ('hospitable'), ('humble'), ('humorous'), ('ideal'),
    ('imaginative'), ('industrious'), ('ingenious'), ('inspired'), ('intrepid'), ('inventive'), ('jaunty'), ('jazzy'),
    ('jolly'), ('jovial'), ('jubilant'), ('keen'), ('kind'), ('kindly'), ('knowing'), ('lean'),
    ('levelheaded'), ('light'), ('likable'), ('limber'), ('lively'), ('loving'), ('loyal'), ('lucky'),
    ('luminous'), ('lush'), ('majestic'), ('mellow'), ('merciful'), ('merry'), ('meticulous'), ('mighty'),
    ('mindful'), ('modest'), ('motivated'), ('neat'), ('nice'), ('nifty'), ('nimble'), ('noble'),
    ('nurturing'), ('obliging'), ('open'), ('optimistic'), ('orderly'), ('outgoing'), ('passionate'), ('patient'),
    ('peaceful'), ('peppy'), ('perky'), ('playful'), ('pleasant'), ('plucky'), ('poised'), ('polite'),
    ('positive'), ('prime'), ('prompt'), ('proud'), ('punctual'), ('quick'), ('quiet'), ('quirky'),
    ('radiant'), ('rare'), ('rational'), ('ready'), ('real'), ('reassuring'), ('refreshing'), ('relaxed'),
    ('reliable'), ('resilient'), ('resourceful'), ('respectful'), ('rich'), ('ripe'), ('robust'), ('rosy'),
    ('rugged'), ('safe'), ('sane'), ('savvy'), ('secure'), ('sensible'), ('serene'), ('sharp'),
    ('shiny'), ('silky'), ('sincere'), ('skillful'), ('sleek'), ('smart'), ('snappy'), ('snug'),
    ('sociable'), ('solid'), ('sound'), ('sparkling'), ('speedy'), ('spiffy'), ('spirited'), ('splendid'),
    ('spotless'), ('spry'), ('spunky'), ('stalwart'), ('steadfast'), ('steady'), ('stellar'), ('strong'),
    ('sturdy'), ('stylish'), ('suave'), ('sunny'), ('super'), ('supportive'), ('sure'), ('sweet'),
    ('swift'), ('tactful'), ('talented'), ('tame'), ('tenacious'), ('thoughtful'), ('thrifty'), ('thriving'),
    ('tidy'), ('tireless'), ('tolerant'), ('tough'), ('tranquil'), ('trim'), ('triumphant'), ('true'),
    ('trusting'), ('trusty'), ('unwavering'), ('upbeat'), ('valiant'), ('vast'), ('versatile'), ('vibrant'),
    ('victorious'), ('vigilant'), ('vigorous'), ('virtuous'), ('vivacious'), ('vivid'), ('warm'), ('warmhearted'),
    ('welcoming'), ('whimsical'), ('wholesome'), ('wild'), ('willing'), ('wise'), ('witty'), ('wonderful'),
    ('worthy'), ('wry'), ('youthful'), ('zany'), ('zealous'), ('zesty'), ('zippy')
ON CONFLICT (word) DO NOTHING;

-- 181 concrete-object "noun" emoji — animals, birds, sea life, insects,
-- plants, sky/nature, food, and everyday objects. Deliberately excludes
-- anything with a common NSFW-adjacent secondary reading (eggplant,
-- peach, banana, hot dog, splashing-sweat, etc.) and non-noun emoji
-- (gestures, flags, faces/smileys, people, activities).
INSERT INTO emoji_nouns (emoji, word) VALUES
    ('🐶', 'dog'), ('🐱', 'cat'), ('🦊', 'fox'), ('🐼', 'panda'),
    ('🦁', 'lion'), ('🐯', 'tiger'), ('🐨', 'koala'), ('🐻', 'bear'),
    ('🐷', 'pig'), ('🐮', 'cow'), ('🐴', 'horse'), ('🦄', 'unicorn'),
    ('🦓', 'zebra'), ('🦌', 'deer'), ('🐘', 'elephant'), ('🦒', 'giraffe'),
    ('🦛', 'hippo'), ('🦏', 'rhino'), ('🐵', 'monkey'), ('🦍', 'gorilla'),
    ('🦘', 'kangaroo'), ('🐑', 'sheep'), ('🐐', 'goat'), ('🐫', 'camel'),
    ('🦙', 'llama'), ('🐰', 'rabbit'), ('🦔', 'hedgehog'), ('🦇', 'bat'),
    ('🐀', 'rat'), ('🐭', 'mouse'), ('🐹', 'hamster'), ('🐺', 'wolf'),
    ('🦦', 'otter'), ('🦨', 'skunk'), ('🦡', 'badger'), ('🦥', 'sloth'),
    ('🦝', 'raccoon'), ('🐿️', 'squirrel'), ('🦬', 'bison'), ('🐔', 'chicken'),
    ('🐓', 'rooster'), ('🐤', 'chick'), ('🐧', 'penguin'), ('🦅', 'eagle'),
    ('🦆', 'duck'), ('🦉', 'owl'), ('🦩', 'flamingo'), ('🦚', 'peacock'),
    ('🦜', 'parrot'), ('🦢', 'swan'), ('🦃', 'turkey'), ('🦤', 'dodo'),
    ('🐳', 'whale'), ('🐬', 'dolphin'), ('🦈', 'shark'), ('🐟', 'fish'),
    ('🐠', 'tropicalfish'), ('🐡', 'pufferfish'), ('🐙', 'octopus'), ('🦑', 'squid'),
    ('🦐', 'shrimp'), ('🦞', 'lobster'), ('🦀', 'crab'), ('🦭', 'seal'),
    ('🐢', 'turtle'), ('🐌', 'snail'), ('🐸', 'frog'), ('🐝', 'bee'),
    ('🦋', 'butterfly'), ('🐞', 'ladybug'), ('🐜', 'ant'), ('🕷️', 'spider'),
    ('🦗', 'cricket'), ('🪲', 'beetle'), ('🪱', 'worm'), ('🦟', 'mosquito'),
    ('🐉', 'dragon'), ('🦕', 'sauropod'), ('🦖', 'dinosaur'), ('🌲', 'tree'),
    ('🌴', 'palmtree'), ('🌵', 'cactus'), ('🌱', 'seedling'), ('🌿', 'herb'),
    ('🍀', 'clover'), ('🌻', 'sunflower'), ('🌷', 'tulip'), ('🌹', 'rose'),
    ('🌺', 'hibiscus'), ('🌸', 'blossom'), ('🍄', 'mushroom'), ('🌰', 'chestnut'),
    ('🍁', 'mapleleaf'), ('🍃', 'leaves'), ('🌙', 'moon'), ('🌈', 'rainbow'),
    ('🚀', 'rocket'), ('🪐', 'planet'), ('⭐', 'star'), ('🍎', 'apple'),
    ('🍐', 'pear'), ('🍊', 'orange'), ('🍋', 'lemon'), ('🍉', 'watermelon'),
    ('🍇', 'grapes'), ('🍓', 'strawberry'), ('🍒', 'cherries'), ('🍍', 'pineapple'),
    ('🥭', 'mango'), ('🥥', 'coconut'), ('🥝', 'kiwi'), ('🍅', 'tomato'),
    ('🥕', 'carrot'), ('🌽', 'corn'), ('🥔', 'potato'), ('🍞', 'bread'),
    ('🥐', 'croissant'), ('🥨', 'pretzel'), ('🧀', 'cheese'), ('🥞', 'pancakes'),
    ('🍯', 'honey'), ('🍪', 'cookie'), ('🧁', 'cupcake'), ('🍩', 'donut'),
    ('🍦', 'icecream'), ('🍕', 'pizza'), ('🌮', 'taco'), ('🍿', 'popcorn'),
    ('🍬', 'candy'), ('🍭', 'lollipop'), ('🍫', 'chocolate'), ('🎈', 'balloon'),
    ('🎁', 'gift'), ('🪁', 'kite'), ('⚓', 'anchor'), ('🧭', 'compass'),
    ('🔑', 'key'), ('🏮', 'lantern'), ('💡', 'lightbulb'), ('🔭', 'telescope'),
    ('🧲', 'magnet'), ('💎', 'gem'), ('👑', 'crown'), ('🏆', 'trophy'),
    ('🏅', 'medal'), ('🥁', 'drum'), ('🎸', 'guitar'), ('🎺', 'trumpet'),
    ('🎻', 'violin'), ('🪕', 'banjo'), ('🎨', 'palette'), ('🎭', 'masks'),
    ('🎪', 'circustent'), ('🎡', 'ferriswheel'), ('🎢', 'rollercoaster'), ('🧩', 'puzzlepiece'),
    ('🪀', 'yoyo'), ('🪃', 'boomerang'), ('🎯', 'dartboard'), ('🧸', 'teddybear'),
    ('🪆', 'nestingdoll'), ('🕯️', 'candle'), ('📚', 'books'), ('📖', 'book'),
    ('✏️', 'pencil'), ('🖊️', 'pen'), ('🔔', 'bell'), ('🎵', 'musicnote'),
    ('🎶', 'notes'), ('🌊', 'wave'), ('🏔️', 'mountain'), ('🏝️', 'island'),
    ('🗻', 'fuji'), ('🌋', 'volcano'), ('🧊', 'ice'), ('🔥', 'flame'),
    ('💧', 'droplet'), ('🍂', 'fallenleaf'), ('🐦', 'bird'), ('🐕', 'puppy'),
    ('🐈', 'kitten')
ON CONFLICT (emoji) DO NOTHING;

-- accounts: phone_number (globally unique, nullable), two visibility
-- toggles, home/work coordinates, and the public-username machinery.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS phone_number TEXT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS show_public_username BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS show_in_leaderboards BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS home_lat DOUBLE PRECISION CHECK (home_lat BETWEEN -90 AND 90);
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS home_lng DOUBLE PRECISION CHECK (home_lng BETWEEN -180 AND 180);
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS work_lat DOUBLE PRECISION CHECK (work_lat BETWEEN -90 AND 90);
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS work_lng DOUBLE PRECISION CHECK (work_lng BETWEEN -180 AND 180);
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS username_adjective TEXT REFERENCES sfw_adjectives(word);
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS username_emoji TEXT REFERENCES emoji_nouns(emoji);
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS public_username TEXT
    GENERATED ALWAYS AS (username_adjective || username_emoji) STORED;

-- email's original NOT NULL (sql/012) no longer holds — a phone-only
-- profile is now valid. DROP NOT NULL on an already-nullable column is a
-- no-op, not an error, so this is safe to re-run.
ALTER TABLE accounts ALTER COLUMN email DROP NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'accounts_phone_number_key'
          AND conrelid = 'accounts'::regclass AND contype = 'u'
    ) THEN
        ALTER TABLE accounts
            ADD CONSTRAINT accounts_phone_number_key UNIQUE (phone_number);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'accounts_public_username_key'
          AND conrelid = 'accounts'::regclass AND contype = 'u'
    ) THEN
        ALTER TABLE accounts
            ADD CONSTRAINT accounts_public_username_key UNIQUE (public_username);
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'accounts_email_or_phone_required'
          AND conrelid = 'accounts'::regclass AND contype = 'c'
    ) THEN
        ALTER TABLE accounts
            ADD CONSTRAINT accounts_email_or_phone_required
            CHECK (email IS NOT NULL OR phone_number IS NOT NULL);
    END IF;
END $$;
