# Low-powered Kid Pager

Using expensive smart watches with also expensive cellular plans just so kids
can message their parents is ridiculous.

This is an attempt to build a simple pager-style device that can be fitted
with an ultra-cheapo SIM card (for example a low-cost data and messaging plan). It should allow
sending very simple messages and location to a custom endpoint and hopefully run for weeks on
one charge.

What works end to end: a message typed in the web app reaches the pager over LTE-M and shows on its
screen, the pager acknowledges it, and replies typed on the pager reach the web app. A page sent to a
pager that is asleep is delivered. Pagers can use an ordinary data SIM or a Soracom SIM, which is
carried through Soracom Beam (`docs/SORACOM_DESIGN.md`).


