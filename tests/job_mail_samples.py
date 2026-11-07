"""Job mail shaped like what Greenhouse, Lever, Ashby and recruiters send, for tests."""

from __future__ import annotations

from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from tests.fake_gmail import ME


def job_mail(sender: str, subject: str, body: str, *, sent: datetime, reply_to: str | None = None,
             html: str | None = None, headers: dict | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ME
    msg["Subject"] = subject
    msg["Date"] = format_datetime(sent)
    msg["Message-ID"] = make_msgid(domain="example.com")
    if reply_to:
        msg["Reply-To"] = reply_to
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def greenhouse_confirmation(sent, title="Senior Backend Engineer, Platform"):
    return job_mail(
        "Example Corp <no-reply@us.greenhouse-mail.io>", "Thank you for applying to Example Corp",
        f"Hi Willy,\n\nThanks for applying to Example Corp! Your application for the {title} role has been "
        "received and our team will review it. If your experience is a match, we'll be in touch about next "
        "steps.\n\nBest,\nExample Corp Recruiting", sent=sent)


def recruiter_invite(sent):
    return job_mail(
        "Jane Doe <jane.doe@examplecorp.com>", "Senior Backend Engineer, Platform - next steps",
        "Hi Willy,\n\nThanks for applying! I'd love to set up a 30 minute call to talk about the Senior Backend "
        "Engineer role. Could you send me your availability for next week?\n\nJane\n\n"
        "On Thu, Nov 5, 2026 at 9:00 AM Willy <me@gmail.com> wrote:\n> We'd love to offer more details\n",
        sent=sent)


def greenhouse_rejection(sent):
    return job_mail(
        "Example Corp <no-reply@us.greenhouse-mail.io>", "Your application to Example Corp",
        "Hi Willy,\n\nThank you for your interest in the Staff Infrastructure Engineer position. After careful "
        "review, we've decided to move forward with other candidates whose experience more closely matches "
        "our needs.\n\nhttps://job-boards.greenhouse.io/examplecorp/jobs/4412990\n", sent=sent)


def newsletter(sent):
    return job_mail("Example Corp <news@examplecorp.com>", "What's new at Example Corp",
                    "Our biggest launch yet. We're excited to offer you 20% off.", sent=sent,
                    headers={"List-Unsubscribe": "<mailto:unsubscribe@examplecorp.com>"})


def ats_unknown(sent):
    return job_mail("Example Corp <no-reply@us.greenhouse-mail.io>", "Please complete your profile",
                    "Hi Willy, please complete your candidate profile for Example Corp.", sent=sent)


def ashby_offer(sent, company="Orbit"):
    return job_mail(f"{company} Hiring <no-reply@ashbyhq.com>", f"Your offer from {company}",
                    f"Hi Willy,\n\nWe're delighted to extend you an offer to join {company}! Your offer letter is "
                    "attached.\n", sent=sent, reply_to="talent@orbit.dev")


def duckduckgo_confirmation(sent):
    """The real one, from Ashby (Nov 7, 2026), name changed."""
    return job_mail(
        "DuckDuckGo Hiring Team <no-reply@ashbyhq.com>", "Senior Backend Engineer - Confirmation of your application",
        "Hi Will,\n\nThanks for your interest in joining us here at DuckDuckGo. Now that you've submitted your info, "
        "our team will review your application.\n\nWe aim to make privacy simple, and the next steps are pretty "
        "simple, too: if you're a potential match for this role, we'll set up a time to chat. If we don't see a fit "
        "for this particular role, we'll still let you know. Keep an eye out for more from us soon.\n\n"
        "If you haven't already, read more about our hiring process here, and feel free to check out our blog to see "
        "what the rest of the team is up to.\n\nThanks,\nThe DuckDuckGo Team\n", sent=sent,
        html='<p>Hi Will,</p><p>Thanks for your interest in joining us here at DuckDuckGo. Now that you\'ve submitted '
             'your info, our team will review your application.</p><p>We aim to make privacy simple, and the next '
             'steps are pretty simple, too: if you\'re a potential match for this role, we\'ll set up a time to '
             'chat. If we don\'t see a fit for this particular role, we\'ll still let you know.</p><p>If you '
             'haven\'t already, read more about our hiring process <a href="https://duckduckgo.com/hiring">here</a>, '
             'and feel free to check out our <a href="https://spreadprivacy.com">blog</a>.</p>')
