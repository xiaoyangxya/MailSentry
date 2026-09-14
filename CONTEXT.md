# MailSentry

MailSentry identifies noteworthy incoming email across personally controlled mailboxes and alerts the owner without changing the source mailbox.

## Language

**Monitored Account**:
An external mailbox that MailSentry observes using read-only access.
_Avoid_: User, inbox, tenant

**Mail Candidate**:
A newly observed message whose importance has not yet been decided.
_Avoid_: Email event, raw email

**Assessment**:
The score, alert level, and human-readable reasons assigned to a Mail Candidate.
_Avoid_: Prediction, spam verdict

**Alert Level**:
The operational outcome of an Assessment: immediate, digest, or audit-only.
_Avoid_: Priority, severity

**Notification Event**:
A durable request to deliver an Assessment through a configured notification channel.
_Avoid_: Message, alert

**Feedback**:
The owner's correction that a prior Assessment was important or not important.
_Avoid_: Label, training example
