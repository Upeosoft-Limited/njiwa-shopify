# Njiwa for Shopify

WhatsApp your customers when their order is paid, fulfilled, cancelled or
refunded, and get a message yourself when one comes in.

## This one is not a drop-in

Shopify does not run other people's code inside a shop. This is a small web
service that Shopify talks to, so before a shop can use it, somebody has to put
it somewhere with a **public HTTPS address** and keep it running: a small
server, a container host, anything that can hold a Python process and a file.
Shopify will not send a webhook to `http://`, and it will not send one to a
laptop.

If that sentence sounds like somebody else's job, it probably is. Hand this
page to whoever looks after your servers, or email hello@upeo.ai.

One running copy serves as many shops as you install it on. Each shop has its
own key, its own numbers and its own wording.

## Install

Python 3.11 or newer.

### 1. Create the app in the Shopify Partner Dashboard

At [partners.shopify.com](https://partners.shopify.com), Apps → Create app →
Create app manually. Then in the app's settings:

| Field | What to put |
| --- | --- |
| App URL | Your public address, for example `https://njiwa.example.com` |
| Allowed redirection URL(s) | The same address with `/auth/callback` on the end |
| Embed app in Shopify admin | Off. This app opens as its own page. |

Under **App setup → Compliance webhooks**, put your address with
`/webhooks/shopify` on the end in all three boxes: customer data request,
customer redact, shop redact. Shopify requires every app to answer those three,
and checks them before it will let an app be distributed. This app answers all
three at that one address, and refuses anything whose signature does not match,
which is what Shopify's own check expects to see.

Keep the **Client ID** and **Client secret** from that page. They are the two
values the service cannot start without.

The app asks each shop for one permission, `read_orders`, and nothing else. It
is the first thing a merchant reads on the install screen, so it asks for
nothing it does not use.

### 2. Run it

```
pip install -e .
uvicorn njiwa_shopify.main:create_app --factory --host 0.0.0.0 --port 8000
```

Put it behind whatever terminates TLS for you. `GET /health` answers with the
version and is there for your monitoring.

Everything it needs comes from the environment:

| Variable | |
| --- | --- |
| `SHOPIFY_API_KEY` | Required. The Client ID from the Partner Dashboard. Public. |
| `SHOPIFY_API_SECRET` | Required. The Client secret. It signs every OAuth callback and every webhook, and it is the one value that must never leave the server. |
| `NJIWA_SHOPIFY_APP_URL` | Required. The public https address, including any path prefix it is mounted under. It must match the Partner Dashboard to the character, because the redirect and every webhook subscription are built from it. |
| `NJIWA_SHOPIFY_DATABASE_URL` | Defaults to `sqlite:///./njiwa-shopify.db` in the working directory. Point it at a path that survives a restart, or at Postgres. |
| `NJIWA_SHOPIFY_SESSION_SECRET` | Signs the browser cookies. Left empty, the Shopify secret is used under a separate salt, which is enough for one process on one host. Set it if you run more than one. |
| `NJIWA_SHOPIFY_ENV` | `production` or `development`. Defaults to `development`. |
| `NJIWA_SHOPIFY_LOG_LEVEL` | Defaults to `INFO`. |

The database is not a cache. It holds each shop's access token, each shop's
settings and the record of what has been sent, and that record is what stops a
customer being messaged twice. Back it up, and do not let a redeploy start it
empty.

### 3. Install it on a shop

Open

```
https://your-app-address/?shop=example.myshopify.com
```

and approve the permission screen. That is the whole install: it stores the
shop's token, reads the shop's name and currency, subscribes to the webhooks
below, and drops you on the settings page.

Afterwards the merchant gets back in through **Apps** in their Shopify admin,
which opens the same settings page already signed in.

## Set it up

Paste your API key from [console.upeo.ai](https://console.upeo.ai) → API keys
and save, then press **Test connection**. It lists the WhatsApp numbers your
Njiwa account actually has, and says which one is the default, so you find out
now rather than at the moment a customer should have been messaged.

**Start with a test key.** A key beginning `sk_test_` checks and stores every
message and delivers nothing. Turn on the events you want, then use **Send me a
test message** below, or place a real order, and read the **Recent messages**
table at the bottom of the settings page. Only then swap in the `sk_live_` key.
A live key sends real messages, and real messages cost money.

An order Shopify marks as a test — the Bogus gateway, or a payment provider's
test mode — sends nothing by default. If you want those messaged while you set
things up, tick **Message test orders too**.

**Send me a test message** takes one number and sends one fixed message to it,
with the key as saved, and tells you exactly what Njiwa said. It is the one
that proves the whole path, all the way to a phone in somebody's hand. Ten an
hour per shop.

Both buttons use the settings as they are *saved*, not as they are on screen.
Save first, then check.

Every field on the page explains itself; the short version:

| Setting | What it is for |
| --- | --- |
| Send WhatsApp messages | The master switch. Off keeps every setting and sends nothing. |
| API key | `sk_test_` delivers nothing, `sk_live_` sends for real. Tick **Forget the saved key** to remove it. |
| Njiwa address | Leave it alone unless you were given your own. It has to be https. |
| Send from | Which of your numbers sends, digits only, in full international form. Empty means the account default. |
| Message test orders too | Off. An order Shopify marked as a test sends nothing, and the log says so. Tick it only while you are watching what comes out. |
| Each event | A tick box and the exact wording. Empty wording sends nothing, whatever the tick box says. |
| Tell me about new orders | The one message that comes to you. |
| Your WhatsApp numbers | Where that message goes. Several, comma separated. Everybody listed gets their own copy. |

## What gets sent, and when

| When | Who hears about it |
| --- | --- |
| An order comes in still waiting for payment | The customer: we have your order, waiting for payment |
| The order is paid | The customer: payment received, getting it ready |
| The order is fulfilled | The customer: it is on its way |
| The order is cancelled | The customer: cancelled |
| A refund is made | The customer: the money is coming back, with the amount |
| An order comes in | You: a new order came in |

Each one is off until you turn it on.

**Order placed** is only sent when the order arrived waiting for money: bank
deposit, cash on delivery, any manual method. An order paid by card at the
checkout gets **Payment received** a moment later instead, because telling
somebody "we are waiting for your payment" when they have already paid reads
like a mistake.

The message to you is sent **once per order**, when the order comes in. On
Shopify an order exists only once a checkout has completed, so an abandoned
cart never wakes you up.

**Refunded** is sent for every refund, partial ones included, and
`{refund_total}` is the amount of that refund rather than the order total. Two
partial refunds are two real events and send two messages.

**Test orders send nothing.** Shopify marks an order `test` when it came
through the Bogus gateway or a payment provider's test mode, and this app
skips those: the phone number on one is still a real phone number, usually
somebody who did not order anything, and a live key would message them and
charge you for it. Each one that is skipped writes a line to the log, so a
quiet test order is explained rather than mysterious. **Message test orders
too**, in the settings, turns them back on for a shop that wants them.

### The webhooks it subscribes to

Registered on the shop at install, all pointing at
`https://your-app-address/webhooks/shopify`:

`orders/create` · `orders/paid` · `orders/fulfilled` · `orders/cancelled` ·
`refunds/create` · `app/uninstalled`

There is no `orders/updated` on purpose: it fires when somebody corrects an
address, and a customer would be messaged for it.

The three compliance topics — `customers/data_request`, `customers/redact` and
`shop/redact` — are not subscribed to through the API. Their address is the one
you set in the Partner Dashboard, and they arrive at the same endpoint.

## The wording

Plain text with placeholders in braces. The settings page lists them all; they
are `{first_name}`, `{last_name}`, `{customer_name}`, `{order_number}`,
`{order_total}`, `{order_date}`, `{order_status}`, `{payment_method}`,
`{items}`, `{item_count}`, `{shop_name}`, `{order_url}`, `{admin_url}` and
`{refund_total}`.

`{order_url}` opens the customer's own order status page. `{admin_url}` opens
the order in your Shopify admin, so it belongs in the message to you and
nowhere else. `{refund_total}` is filled in only on the refund message and is
empty everywhere else.

A placeholder that does not exist, `{order_no}` say, is removed before sending
rather than posted to a customer, and a line is written to the log telling you
where to look.

## Things worth knowing

**Shopify is never kept waiting.** Shopify allows an endpoint five seconds and
retries anything that is not a 200. This app decides what to send, writes it
down, answers Shopify, and sends afterwards. Njiwa being slow cannot cause a
webhook to be sent again.

**Nothing is sent twice.** Three things see to it: every webhook id Shopify
delivers is remembered, so a redelivery is recognised; a row is claimed per
shop, order, event and recipient before anything is sent, and a second attempt
at the same one stops there; and each message carries an idempotency key that
Njiwa honours for twenty-four hours.

**A restart does not lose a message.** A message is written down before it is
sent and marked afterwards, so anything caught in between is picked up and sent
again when the service starts.

**A network failure is retried three times. A refusal is not.** If Njiwa says
no — no credit, a number that is not linked, a recipient WhatsApp does not know
— the reason is written against that message on the settings page, and that is
the end of it. It would say no again.

**Phone numbers are read against the order's country.** `0712345678` on an
order shipping to Kenya becomes `254712345678`. A number already written in
full is left alone. Anything with an `@` in it is refused outright, because
that is how a WhatsApp group is addressed and this app must never post to a
group. A customer with no phone number is normal: nothing is sent, and the
settings page says why.

**Recent messages** shows the last fifty, with the order, the event, the last
four digits of the number and what became of it. The message text itself is
dropped the moment Njiwa has it, and only those four digits of the recipient
are kept.

**Uninstalling removes the key.** When somebody deletes the app from a shop,
Shopify says so and the token and every setting, the Njiwa key included, are
cleared straight away. Shopify sends `shop/redact` about 48 hours later, and
that removes the rest.

**Refunds on old orders.** A refund webhook names its order and says nothing
about the customer, so the order is fetched. Shopify will not hand an app with
`read_orders` an order more than sixty days old; when that happens, no message
is sent and the log says so.

## What it does not do

**It does not receive replies.** Inbound WhatsApp arrives as a webhook and
verifying one needs that number's signing secret, which the console does not
yet show. Until it does, a receiving feature could not check that a request
really came from Njiwa, so there is not one.

**It does not live inside the Shopify admin.** It opens as its own page from
Apps rather than embedded in the admin frame.

**It does not keep a copy of your messages.** Njiwa already stores every
message, its status and its failure reason, and a second copy is a second thing
to keep in step.

**It does not run campaigns.** Bulk sending to past customers is what the Njiwa
console is for, on Business plans and above.

---

Docs: https://docs.njiwa.upeo.ai · Console: https://console.upeo.ai
UPEO.AI · hello@upeo.ai · 0116888777 on WhatsApp
