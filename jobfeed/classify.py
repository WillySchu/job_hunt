"""What a job email means, from its sender, subject and opening lines.

Kinds: confirmation, rejection, interview, offer, and reply (a person at the
company wrote, with none of the other wording). Each rule is a phrase pattern,
and the phrase that matched is kept, so `jobfeed mail --dry-run` can show why a
message was read the way it was. Mail that fits no kind, or two that don't
usually go together, is left for you to check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import parseaddr

from jobfeed.replies import _STOP, body_text

OPENING_CHARS = 2500

_P = lambda *ps: [re.compile(p, re.I) for p in ps]  # noqa: E731

OFFER = _P(
    r"\bpleased to (extend|offer)\b",
    r"\boffer letter\b",
    r"\b(excited|happy|delighted|thrilled) to (extend|offer) you\b",
    r"\bextend (you )?an offer\b",
    r"\bverbal offer\b",
    r"\boffer of employment\b",
    r"\bformal offer\b",
)
REJECTION = _P(
    r"\bnot (be )?moving forward with (your|you)\b",
    r"\b(will not|won'?t|are not|aren'?t) (be )?(moving|proceeding) forward\b",
    r"\b(decided|chosen|decision) to (move|proceed|go) forward with (other|another|a different)\b",
    r"\b(pursue|pursuing|proceed with|move forward with|moving forward with) other (candidates|applicants)\b",
    r"\b(decided|chosen) not to (move|proceed|pursue|advance)\b",
    r"\b(are|were) not (able|in a position) to (move|offer|proceed|advance)\b",
    r"\bposition has (now )?been filled\b",
    r"\bno longer (considering|being considered|under consideration)\b",
    r"\b(have|has) not been selected\b",
    r"\bregret to inform\b",
    r"\bunable to offer you\b",
    # "candidates whose skills..." is a rejection only after a decision: "we'll only reach out to candidates whose
    # skills align" is how some confirmations (Upstart's) explain what happens next.
    r"\b(move|moving|proceed|proceeding|go|going|continue|continuing) forward with (other |a few |the )?"
    r"(candidates|applicants) whose\b",
    r"\b(pursue|pursuing|selected|chosen|chose|focus on|focusing on) (other |a few )?(candidates|applicants) whose\b",
    r"\bnot (to )?(advance|progress) your (application|candidacy)\b",
)
INTERVIEW = _P(
    r"\byour availability\b",
    r"\b(like|love|want) to (schedule|set up|arrange|book)\b",
    r"\binvite you to (a |an |our )?(interview|call|chat|phone|conversation|technical|onsite|virtual)",
    r"\b(invitation|invite) to (an? )?interview\b",
    r"\binterview (request|invitation)\b",
    r"\b(would|'d) (like|love) to (chat|talk|speak|connect|meet)\b",
    r"\b(move|moving) (you )?(forward )?to the next (step|stage|round)\b",
    r"\blet (me|us) know (a few |some )?times that work\b",
    r"\bwhen (are|would) you (be )?(free|available)\b",
    r"\bplease (use|click|select|choose|pick) .{0,60}\b(link|time|slot)\b",
    r"calendly\.com/", r"goodtime\.io", r"ashbyhq\.com/[^ ]*schedul", r"\bschedule your (interview|call)\b",
)
CONFIRMATION = _P(
    r"\bthank(s| you) for (applying|your application|submitting your application)\b",
    r"\b(we('ve| have)|we) received your (application|resume)\b",
    r"\bapplication (has been |was )?(received|submitted)\b",
    r"\byour application (to|for) .{0,80} (has been|was) (received|submitted)\b",
    r"\bsuccessfully (applied|submitted)\b",
    r"\bconfirmation of your application\b",
    r"\byou('ve| have) (successfully )?submitted (your|an) (info|information|application)\b",
    r"\bthank(s| you) for (your )?interest in\b",
)
RULES = [("offer", OFFER), ("rejection", REJECTION), ("interview", INTERVIEW), ("confirmation", CONFIRMATION)]

_ROBOT = re.compile(r"^(no-?reply|do-?not-?reply|donotreply|notifications?|notify|mailer|bounces?|news(letter)?|"
                    r"marketing|updates?|info|hello|team|support|careers?-?noreply|jobs?-?noreply|alerts?)\b", re.I)


@dataclass
class Reading:
    kind: str | None            # confirmation | rejection | interview | offer | reply | None (check it)
    why: str                    # the phrase that decided it, or why nothing did
    kinds: list[str] = field(default_factory=list)   # every kind whose wording appeared


@dataclass
class JobMail:
    """The parts of a message the rules look at. Nothing here is stored."""
    sender: str                 # address, lower case
    name: str                   # display name
    reply_to: str               # address, lower case, or ""
    subject: str
    opening: str                # subject plus the first lines of the body, quoted text removed
    links: list[str]            # URLs in the body
    bulk: bool                  # a mailing list or newsletter


_URL = re.compile(r"https?://[^\s<>\"')\]]+")


def unquoted(body: str) -> str:
    out = []
    for line in body.splitlines():
        if any(p.search(line.strip()) for p in _STOP):
            break
        out.append(line)
    return "\n".join(out)


def read_message(msg: EmailMessage) -> JobMail:
    name, sender = parseaddr(str(msg.get("From") or ""))
    reply_to = parseaddr(str(msg.get("Reply-To") or ""))[1]
    subject = str(msg.get("Subject") or "")
    body = body_text(msg)
    raw_html = ""
    html_part = msg.get_body(preferencelist=("html",))
    if html_part is not None:
        try:
            raw_html = html_part.get_content()
        except (LookupError, ValueError):
            raw_html = ""
    text = unquoted(body)
    opening = (subject + "\n" + re.sub(r"[ \t]+", " ", text))[:OPENING_CHARS]
    links = [u.rstrip(".,;") for u in _URL.findall(body + "\n" + raw_html)]
    bulk = bool(msg.get("List-Unsubscribe") or msg.get("List-Id")) or \
        (msg.get("Precedence") or "").strip().lower() in ("bulk", "list")
    return JobMail(sender=sender.lower(), name=name, reply_to=reply_to.lower(), subject=subject, opening=opening,
                   links=links, bulk=bulk)


def domain(address: str) -> str:
    return address.rpartition("@")[2].lower()


def is_ats(address: str, ats_domains: list[str]) -> bool:
    d = domain(address)
    return any(d == a or d.endswith("." + a) for a in ats_domains)


def is_person(address: str) -> bool:
    return bool(address) and not _ROBOT.match(address.partition("@")[0])


def classify(mail: JobMail, *, from_ats: bool) -> Reading:
    if mail.bulk and not from_ats:
        return Reading(None, "a newsletter or mailing list")   # "excited to offer you 20% off" is not an offer
    found: dict[str, str] = {}
    text = mail.opening + "\n" + " ".join(mail.links)
    for kind, patterns in RULES:
        for p in patterns:
            m = p.search(text)
            if m:
                found[kind] = m.group(0)
                break
    kinds = list(found)
    if "offer" in found and "rejection" not in found:
        return Reading("offer", found["offer"], kinds)
    if "rejection" in found and "offer" not in found:
        # Rejections thank you for applying and mention next steps; the "no" decides.
        return Reading("rejection", found["rejection"], kinds)
    if "interview" in found and "confirmation" in found:
        # "Thanks for applying! I'd love to set up a call" from a recruiter is an invitation;
        # the same words from a no-reply address are too unclear to act on.
        if not from_ats and is_person(mail.sender):
            return Reading("interview", found["interview"], kinds)
        return Reading(None, f"reads as both a confirmation (“{found['confirmation']}”) and an interview "
                             f"request (“{found['interview']}”)", kinds)
    if len(found) == 1:
        kind = kinds[0]
        return Reading(kind, found[kind], kinds)
    if found:
        return Reading(None, "reads as " + " and ".join(f"{k} (“{v}”)" for k, v in found.items()), kinds)
    if not from_ats and not mail.bulk and is_person(mail.sender):
        return Reading("reply", f"a message from {mail.sender}", kinds)
    return Reading(None, "no wording it knows", kinds)
