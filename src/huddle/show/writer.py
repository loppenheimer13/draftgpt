"""Writing the show.

This module is the difference between a sportscast and a database readout. It
takes the brief and produces broadcast copy: two named hosts, real transitions,
scores told as small stories, and explanations pitched at the listener's age.

Five rules shape everything here.

**Two hosts, one per track.** Yoto synthesises a track with a single voice, so
the track boundary *is* the turn boundary. Each track ends with a handoff
written in the current host's voice and the next opens in the other's, which is
exactly how real radio hands over -- and it costs nothing to produce.

**Say it the way a person says it.** "The Braves beat the Nationals five to
three" -- not "ATL 5, WSH 3". Numbers become words, abbreviations become names.

**Explain, briefly, once.** The listener is a child. A term gets a half-sentence
of context the first time it appears and never again in the same show.

**Never fake enthusiasm about nothing.** If no followed team played, the show
says so cheerfully and moves to what it does have. Manufactured excitement over
an empty schedule is the fastest way to lose a young listener's trust.

**Fit the runtime.** A show promised at five minutes that runs nine is broken.
The writer budgets characters per segment and trims the least important
material first, rather than truncating mid-sentence at the end.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

from huddle.domain.enums import (
    CHARS_PER_MINUTE,
    CONDITIONAL_SEGMENTS,
    HOST_PROFILES,
    SEGMENT_ORDER,
    SEGMENT_TITLES,
    Host,
    ShowSegment,
)
from huddle.show.brief import BirthdayLine, GameLine, ShowBrief, TeamUpdate
from huddle.yoto.content import LABS_TRACK_CHAR_LIMIT

logger = logging.getLogger(__name__)

#: The curated segments are a fixed length by nature -- one moment, one
#: factoid, a short birthday list -- so they get a cap rather than a share.
FIXED_SEGMENT_CAPS: dict[str, int] = {
    ShowSegment.BIRTHDAY_CLUB: 420,
    ShowSegment.ON_THIS_DAY: 520,
    ShowSegment.ROOKIE_FACTOID: 480,
    ShowSegment.WEEKEND_EDITION: 520,
}

#: Whatever runtime the fixed segments do not use is split between these two,
#: which can always say more if there is more to say. Without this the show
#: silently under-runs its promised length on a quiet day.
ELASTIC_WEIGHTS: dict[str, float] = {
    ShowSegment.YOUR_TEAMS: 0.62,
    ShowSegment.TODAY_IN_SPORTS: 0.38,
}

#: Which host leads each segment. Nova explains, Rae reacts -- so the teaching
#: segments go to Nova and the celebratory ones to Rae.
SEGMENT_HOSTS: dict[str, str] = {
    ShowSegment.YOUR_TEAMS: Host.NOVA,
    ShowSegment.TODAY_IN_SPORTS: Host.RAE,
    ShowSegment.BIRTHDAY_CLUB: Host.RAE,
    ShowSegment.ON_THIS_DAY: Host.NOVA,
    ShowSegment.ROOKIE_FACTOID: Host.NOVA,
    ShowSegment.WEEKEND_EDITION: Host.RAE,
}

NUMBER_WORDS = {
    0: "nothing", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven",
    12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen",
    16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen", 20: "twenty",
}


@dataclass
class WrittenSegment:
    segment: str
    title: str
    host: str
    text: str
    material_inputs: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.text)


def write_show(
    brief: ShowBrief,
    *,
    segments: list[str] | None = None,
    target_minutes: int = 5,
    listener_age: int = 8,
) -> list[WrittenSegment]:
    """Produce the full episode, in broadcast order."""
    wanted = _segments_for_today(segments or list(SEGMENT_ORDER), brief.show_date)
    budget = _budget(wanted, target_minutes)

    writers = {
        ShowSegment.YOUR_TEAMS: _write_your_teams,
        ShowSegment.TODAY_IN_SPORTS: _write_today_in_sports,
        ShowSegment.BIRTHDAY_CLUB: _write_birthday_club,
        ShowSegment.ON_THIS_DAY: _write_on_this_day,
        ShowSegment.ROOKIE_FACTOID: _write_rookie_factoid,
        ShowSegment.WEEKEND_EDITION: _write_weekend_edition,
    }

    written: list[WrittenSegment] = []
    for name in wanted:
        writer = writers.get(ShowSegment(name))
        if writer is None:
            continue
        segment = writer(brief, budget[name], listener_age)
        if segment is not None:
            written.append(segment)

    if not written:
        return []

    _add_opening(written, brief)
    _add_handoffs(written)
    _add_signoff(written[-1], brief)
    return written


# -- planning --------------------------------------------------------------
def _segments_for_today(requested: list[str], on: date) -> list[str]:
    """Drop segments that do not belong on this weekday."""
    weekday = on.weekday()
    chosen = set(requested)
    return [
        name
        for name in SEGMENT_ORDER
        if name in chosen
        and weekday in CONDITIONAL_SEGMENTS.get(name, tuple(range(7)))
    ]


def _budget(segments: list[str], target_minutes: int) -> dict[str, int]:
    """Characters per segment, scaled to the requested runtime.

    Fixed segments take their cap; the elastic ones divide the remainder. So
    turning Birthday Club off makes Your Teams longer rather than making the
    whole show shorter than the parent asked for.
    """
    total_chars = max(target_minutes, 2) * CHARS_PER_MINUTE
    budget = {
        name: FIXED_SEGMENT_CAPS[name] for name in segments if name in FIXED_SEGMENT_CAPS
    }
    elastic = [name for name in segments if name in ELASTIC_WEIGHTS]
    remaining = max(total_chars - sum(budget.values()), 600)

    denominator = sum(ELASTIC_WEIGHTS[name] for name in elastic) or 1.0
    for name in elastic:
        budget[name] = max(400, int(remaining * ELASTIC_WEIGHTS[name] / denominator))
    for name in segments:
        budget.setdefault(name, 400)
    return budget


# -- segment writers -------------------------------------------------------
def _write_your_teams(brief: ShowBrief, budget: int, age: int) -> WrittenSegment | None:
    host = SEGMENT_HOSTS[ShowSegment.YOUR_TEAMS]
    if not brief.teams:
        return _segment(
            ShowSegment.YOUR_TEAMS, host,
            "You haven't picked any teams yet, so there's nothing in this part of "
            "the show today. A grown-up can add your favourites any time, and "
            "then this is where we'll talk about them first.",
            ["teams.none"],
        )

    parts: list[str] = []
    inputs: list[str] = []
    for team in brief.teams:
        block = _team_paragraph(team, age)
        if not block:
            continue
        # Stop adding teams once the segment is full rather than cutting one
        # off mid-thought. A team that misses out leads tomorrow instead.
        if parts and sum(len(p) for p in parts) + len(block) > budget:
            break
        parts.append(block)
        inputs.append(f"team:{team.team_id}")

    if not parts:
        return None
    return _segment(ShowSegment.YOUR_TEAMS, host, " ".join(parts), inputs)


def _team_paragraph(team: TeamUpdate, age: int) -> str:
    """One team's block. Always opens by naming the team, because a listener
    who missed the handoff has no other way to know who this is about."""
    name = _article(team.spoken_name)
    sentences: list[str] = []
    named = False

    if team.last_game is not None and team.last_game.has_score:
        sentences.append(_result_sentence(team, name))
        named = True
    elif team.last_game is not None:
        sentences.append(f"{name} were in action, but the final score isn't in yet.")
        named = True

    if team.next_game is not None:
        subject = "they" if named else name
        sentences.append(_next_game_sentence(team, subject, capitalise=not named))
        named = True
    elif team.last_game is None:
        sentences.append(f"{name} don't have a game on the schedule right now.")
        named = True

    record = _say_record(team.record_summary, team.league_key) if team.record_summary else None
    if record and age >= 7:
        sentences.append(f"Their record this season is {record}.")

    # Skip a "news" line that just retells the result we opened with.
    if team.story and not _story_repeats_result(team):
        sentences.append(f"In {team.spoken_name} news: {_soften(team.story)}.")

    if team.one_thing:
        sentences.append(f"One thing to know: {team.one_thing}")

    return " ".join(sentences)


def _story_repeats_result(team: TeamUpdate) -> bool:
    """True when the team's headline is a recap of the game we just described."""
    game = team.last_game
    if game is None or not team.story:
        return False
    headline = team.story.lower()
    opponent = _perspective(game, team)[2]
    if opponent and opponent.replace("the ", "") .lower() in headline:
        return True
    # A scoreline in the headline matching the one we read out is the same game.
    if game.has_score:
        pair = {str(game.home_score), str(game.away_score)}
        return all(number in headline for number in pair)
    return False


def _article(name: str) -> str:
    """Add "the" only where English does.

    "the Braves" but "Liverpool" -- a plural nickname takes an article and a
    place name does not. Getting this wrong ("the Liverpool") is the kind of
    small error that makes a show sound machine-made.
    """
    last = name.split()[-1] if name else name
    if last.endswith("s") and not last.endswith("ss"):
        return f"the {name}"
    return name


def _result_sentence(team: TeamUpdate, name: str) -> str:
    """Tell the score as a sentence, from the family's point of view."""
    game = team.last_game
    assert game is not None
    ours, theirs, opponent = _perspective(game, team)
    if ours is None or theirs is None:
        return f"The {name} played {opponent or 'their last game'}."

    ours_word, theirs_word = _say(ours), _say(theirs)
    if ours > theirs:
        verb = "squeaked past" if ours - theirs <= 2 else "beat"
        return f"Good news: {name} {verb} {opponent}, {ours_word} to {theirs_word}."
    if ours < theirs:
        margin = theirs - ours
        tail = " It was close, though." if margin <= 2 else ""
        return f"{name} lost to {opponent}, {theirs_word} to {ours_word}.{tail}"
    return f"{name} and {opponent} finished level at {ours_word} apiece."


def _next_game_sentence(team: TeamUpdate, subject: str, *, capitalise: bool) -> str:
    game = team.next_game
    assert game is not None
    _, _, opponent = _perspective(game, team)
    when = game.when or "soon"
    lead = "Next up, " if not capitalise else ""
    sentence = f"{lead}{subject} play {opponent or 'again'} {when}"
    if game.broadcast:
        sentence += f", on {game.broadcast}"
    sentence = sentence + "."
    return sentence[0].upper() + sentence[1:]


def _perspective(
    game: GameLine, team: TeamUpdate
) -> tuple[int | None, int | None, str | None]:
    """Work out which side of the scoreline is 'us'.

    Matching on name rather than id because a scoreboard can carry an opponent
    this database has never ingested, and the show would rather say the right
    thing from a name than nothing from a missing row.
    """
    ours_is_home = _same_team(game.home_name, team)
    if ours_is_home:
        return game.home_score, game.away_score, _short(game.away_name)
    if _same_team(game.away_name, team):
        return game.away_score, game.home_score, _short(game.home_name)
    return None, None, _short(game.home_name or game.away_name)


def _same_team(candidate: str | None, team: TeamUpdate) -> bool:
    if not candidate:
        return False
    text = candidate.lower()
    return team.name.lower() == text or team.short_name.lower() in text


def _short(full_name: str | None) -> str | None:
    """Shorten a club name only where English actually does.

    American franchise names end in a plural nickname, so "Washington
    Nationals" becomes "the Nationals". Soccer clubs do not -- "Manchester
    City" must never become "the City" -- so anything that is not plural is
    said in full.
    """
    if not full_name:
        return None
    parts = full_name.split()
    if len(parts) < 2:
        return full_name
    last = parts[-1]
    if last.endswith("s") and not last.endswith("ss"):
        return f"the {last}"
    return full_name


def _write_today_in_sports(brief: ShowBrief, budget: int, age: int) -> WrittenSegment | None:
    host = SEGMENT_HOSTS[ShowSegment.TODAY_IN_SPORTS]
    if not brief.today_stories:
        return _segment(
            ShowSegment.TODAY_IN_SPORTS, host,
            "It's a quiet one out there today. Nothing enormous happened while "
            "you were asleep, which honestly happens more often than the "
            "highlight shows admit.",
            ["stories.none"],
        )

    parts: list[str] = []
    inputs: list[str] = []
    for story in brief.today_stories:
        headline = _soften(story.headline)
        # A headline that is itself a question keeps its own mark.
        stop = "" if headline.endswith(("?", "!")) else "."
        line = f"From {story.league_spoken}: {headline}{stop}"
        # Some feeds set the summary to the headline verbatim; reading it twice
        # sounds like a stutter.
        summary = _soften(story.summary) if story.summary else ""
        if summary and not _echoes(headline, summary) and len(line) + len(summary) < budget:
            line += f" {_first_sentence(summary)}"
        if parts and sum(len(p) for p in parts) + len(line) > budget:
            break
        parts.append(line)
        inputs.append(f"story:{story.league_key}:{story.headline[:40]}")

    return _segment(ShowSegment.TODAY_IN_SPORTS, host, " ".join(parts), inputs)


def _write_birthday_club(brief: ShowBrief, budget: int, age: int) -> WrittenSegment | None:
    host = SEGMENT_HOSTS[ShowSegment.BIRTHDAY_CLUB]
    if not brief.birthdays:
        return _segment(
            ShowSegment.BIRTHDAY_CLUB, host,
            "Nobody on your teams is blowing out candles today. But somebody, "
            "somewhere, is having a birthday, so let's wish them a happy one anyway.",
            ["birthdays.none"],
        )

    parts = ["Time for the Birthday Club."]
    inputs: list[str] = []
    for person in brief.birthdays:
        parts.append(_birthday_sentence(person))
        inputs.append(f"birthday:{person.name}")
    parts.append("Happy birthday from all of us here.")
    return _segment(ShowSegment.BIRTHDAY_CLUB, host, " ".join(parts), inputs)


def _birthday_sentence(person: BirthdayLine) -> str:
    bits = [person.name]
    if person.position and person.team_name:
        bits.append(f", who plays {_say_position(person.position)} for the {person.team_name}")
    elif person.team_name:
        bits.append(f" of the {person.team_name}")
    sentence = "".join(bits)
    if person.age:
        sentence += f", turns {person.age} today"
    else:
        sentence += " has a birthday today"
    if person.birth_place:
        sentence += f". They're from {person.birth_place}"
    return sentence + "."


def _write_on_this_day(brief: ShowBrief, budget: int, age: int) -> WrittenSegment | None:
    host = SEGMENT_HOSTS[ShowSegment.ON_THIS_DAY]
    moment = brief.moment
    if moment is None:
        return None

    # Only claim an anniversary when it genuinely is one.
    opener = (
        "On this day in sports history: " if moment.is_dated
        else "Here's a moment worth remembering: "
    )
    text = f"{opener}{moment.title}. {moment.story}"
    return _segment(ShowSegment.ON_THIS_DAY, host, text, [f"moment:{moment.title}"])


def _write_rookie_factoid(brief: ShowBrief, budget: int, age: int) -> WrittenSegment | None:
    host = SEGMENT_HOSTS[ShowSegment.ROOKIE_FACTOID]
    factoid = brief.factoid
    if factoid is None:
        return None

    prompt = factoid.question.strip()
    if prompt.endswith("?"):
        text = f"{prompt} {factoid.answer}"
    else:
        text = f"Here's one for you: {prompt}. {factoid.answer}"
    if age <= 6:
        text += " Now you know something most grown-ups don't."
    return _segment(
        ShowSegment.ROOKIE_FACTOID, host, text, [f"factoid:{factoid.question}"]
    )


def _write_weekend_edition(brief: ShowBrief, budget: int, age: int) -> WrittenSegment | None:
    host = SEGMENT_HOSTS[ShowSegment.WEEKEND_EDITION]
    if not brief.weekend_games:
        return _segment(
            ShowSegment.WEEKEND_EDITION, host,
            "Your teams have a quiet weekend coming up, so it's a good one for "
            "playing outside instead of watching.",
            ["weekend.none"],
        )

    parts = ["Here's your weekend."]
    inputs: list[str] = []
    for game in brief.weekend_games:
        home, away = _short(game.home_name), _short(game.away_name)
        line = f"{away} visit {home} {game.when or 'this weekend'}"
        if game.broadcast:
            line += f", on {game.broadcast}"
        parts.append(line + ".")
        inputs.append(f"weekend:{game.home_name}:{game.away_name}")
    parts.append("Pick a side and shout for them.")
    return _segment(ShowSegment.WEEKEND_EDITION, host, " ".join(parts), inputs)


# -- show furniture --------------------------------------------------------
def _add_opening(written: list[WrittenSegment], brief: ShowBrief) -> None:
    """Open in the opposite host's voice to whoever leads segment one, so the
    show always starts as a conversation between two people."""
    first = written[0]
    opener_host = Host.RAE if first.host == Host.NOVA else Host.NOVA
    opener_name = HOST_PROFILES[opener_host]["name"]
    other_name = HOST_PROFILES[first.host]["name"]

    if brief.has_any_sport:
        hook = "there's plenty to get through."
    else:
        hook = "it's a slow one out there, so we've brought the good stuff instead."

    opening = (
        f"Huddle up! It's {brief.weekday}, and this is your sports show. "
        f"I'm {opener_name}, {other_name} is here too, and {hook} "
    )
    # The opener rides on the first track rather than becoming its own, which
    # keeps the show to as few tracks as possible -- every extra track is
    # another gap a young listener has to sit through.
    written[0] = WrittenSegment(
        segment=first.segment,
        title=first.title,
        host=opener_host,
        text=opening + _handover_into(first.segment) + " " + first.text,
        material_inputs=[*first.material_inputs, "show.opening"],
    )


def _add_handoffs(written: list[WrittenSegment]) -> None:
    """End each track by naming what is coming, in the current host's voice."""
    for index in range(len(written) - 1):
        current, nxt = written[index], written[index + 1]
        # Alternate voices where two adjacent segments landed on the same host,
        # so the show never reads as one person talking for four minutes. This
        # has to happen *before* the handoff line is written, or the outgoing
        # host introduces somebody who is not actually up next.
        if nxt.host == current.host:
            nxt.host = Host.RAE if current.host == Host.NOVA else Host.NOVA
        current.text = f"{current.text} {_handoff(nxt.segment, HOST_PROFILES[nxt.host]['name'])}"


def _handover_into(segment: str) -> str:
    return {
        ShowSegment.YOUR_TEAMS: "Let's start where we always start: your teams.",
        ShowSegment.TODAY_IN_SPORTS: "Here's what's happening out there.",
        ShowSegment.BIRTHDAY_CLUB: "First up, the Birthday Club.",
        ShowSegment.ON_THIS_DAY: "Let's begin with a bit of history.",
        ShowSegment.ROOKIE_FACTOID: "Let's start by learning something.",
        ShowSegment.WEEKEND_EDITION: "Let's look ahead to the weekend.",
    }.get(ShowSegment(segment), "Here we go.")


def _handoff(segment: str, next_host: str) -> str:
    return {
        ShowSegment.YOUR_TEAMS: f"{next_host}, how did our teams get on?",
        ShowSegment.TODAY_IN_SPORTS: f"{next_host}, what else is happening today?",
        ShowSegment.BIRTHDAY_CLUB: f"Now, {next_host} — who's celebrating?",
        ShowSegment.ON_THIS_DAY: f"{next_host} has a story from the history books.",
        ShowSegment.ROOKIE_FACTOID: f"Over to {next_host}, who's going to teach us something.",
        ShowSegment.WEEKEND_EDITION: f"{next_host}, what should we watch this weekend?",
    }.get(ShowSegment(segment), f"Over to you, {next_host}.")


def _add_signoff(last: WrittenSegment, brief: ShowBrief) -> None:
    tail = " That's your Huddle for today. Go play something."
    if brief.freshness and brief.freshness.status != "fresh":
        # Say it plainly, without alarming anyone. A child can understand
        # "some scores came in late" perfectly well.
        tail = (
            " A couple of today's scores came in late, so check with a grown-up "
            "if something sounds off." + tail
        )
    if last.char_count + len(tail) <= LABS_TRACK_CHAR_LIMIT:
        last.text += tail


# -- language helpers ------------------------------------------------------
#: Start of the text, or the start of a sentence after terminal punctuation.
_SENTENCE_START = re.compile(r"(^|[.!?]\s+)([a-z])")


def _segment(segment: str, host: str, text: str, inputs: list[str]) -> WrittenSegment:
    return WrittenSegment(
        segment=str(segment),
        title=SEGMENT_TITLES.get(segment, str(segment).replace("_", " ").title()),
        host=str(host),
        text=_capitalise_sentences(" ".join(text.split())),
        material_inputs=inputs,
    )


def _capitalise_sentences(text: str) -> str:
    """Capitalise sentence openings.

    Clauses are assembled from fragments, so a sentence can legitimately begin
    with an article added late ("the Braves lost..."). Fixing it here rather
    than at every call site means no fragment builder has to know whether it
    will land at the start of a sentence.
    """
    return _SENTENCE_START.sub(lambda m: m.group(1) + m.group(2).upper(), text)


def _say(value: int | None) -> str:
    """Scores as words. A synthesiser reads "5" fine but "5-3" badly."""
    if value is None:
        return "no score"
    if value in NUMBER_WORDS:
        return NUMBER_WORDS[value]
    return str(value)


#: Sports whose record summary is win-draw-loss rather than win-loss, where the
#: middle number cannot be read as "losses" without being wrong.
_THREE_PART_LEAGUES = {"epl", "mls", "nwsl", "laliga", "ucl", "ligamx", "nhl"}


def _say_record(summary: str | None, league_key: str) -> str | None:
    """"82-57" -> "82 wins and 57 losses".

    A three-part record means different things in different sports -- soccer
    writes win-draw-loss, hockey writes win-loss-overtime -- so rather than
    guess and be wrong, those are left unspoken and the standing carries the
    story instead.
    """
    if not summary:
        return None
    parts = [p.strip() for p in summary.replace("–", "-").split("-")]
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return f"{_plural(parts[0], 'win')} and {_plural(parts[1], 'loss', 'losses')}"
    if (
        len(parts) == 3
        and league_key not in _THREE_PART_LEAGUES
        and all(p.isdigit() for p in parts)
    ):
        return (
            f"{_plural(parts[0], 'win')}, {_plural(parts[1], 'loss', 'losses')} "
            f"and {_plural(parts[2], 'draw')}"
        )
    return None


def _plural(count: str, singular: str, plural: str | None = None) -> str:
    word = singular if count == "1" else (plural or f"{singular}s")
    return f"{'no' if count == '0' else count} {word}"


def _say_position(position: str) -> str:
    return position.lower()


#: "6-3" in a headline. A synthesiser reads the hyphen as "dash".
_SCORELINE = re.compile(r"\b(\d{1,3})\s*[-–]\s*(\d{1,3})\b")
#: A bare small number reads better as a word in the middle of a sentence.
_SMALL_NUMBER = re.compile(r"(?<![\w.-])(\d{1,2})(?![\w.:-])")


def _soften(headline: str) -> str:
    """Rewrite wire copy so it can be spoken.

    Headlines use colons, pipes, and dashes to do structural work a voice
    cannot convey, and write scores as "6-3", which a synthesiser reads as
    "six dash three". Apostrophes are kept -- stripping them turns "won't"
    into "wont", which is worse than the punctuation ever was.
    """
    text = headline.strip().rstrip(".")
    for marker in (" -- ", " — ", " – "):
        text = text.replace(marker, ", ")
    text = text.replace(" | ", ", ").replace('"', "").replace("\u201c", "").replace("\u201d", "")
    text = _SCORELINE.sub(lambda m: f"{_say(int(m.group(1)))} to {_say(int(m.group(2)))}", text)
    text = _SMALL_NUMBER.sub(lambda m: NUMBER_WORDS.get(int(m.group(1)), m.group(1)), text)
    return " ".join(text.split())


def _first_sentence(text: str) -> str:
    """One sentence of context, always ending in a full stop.

    Wire summaries are often truncated mid-sentence with no terminator; without
    a period the next line runs straight into it and the voice never pauses.
    """
    stripped = text.strip().lstrip("—- ")
    for end in (". ", "! ", "? "):
        index = stripped.find(end)
        if index > 30:
            return stripped[: index + 1].strip()
    if len(stripped) >= 220:
        stripped = stripped[:217].rsplit(" ", 1)[0]
    return stripped if stripped.endswith((".", "!", "?")) else stripped + "."


def _echoes(headline: str, summary: str) -> bool:
    """True when a summary just repeats its headline."""
    a, b = headline.lower().rstrip("."), summary.lower().rstrip(".")
    return a == b or a.startswith(b[:60]) or b.startswith(a[:60])


def estimate_minutes(segments: list[WrittenSegment]) -> float:
    return round(sum(s.char_count for s in segments) / CHARS_PER_MINUTE, 1)
