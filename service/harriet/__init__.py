"""Harriet compat shim — the contract that shipped first, frozen.

`POST /extract`, `POST /nest` and `GET /health` keep their exact original paths,
request fields and response shapes (camelCase `NestResult`, mapped onto
packages/domain/src/nesting/types.ts) so Harriet needs no coordinated deploy.

It is a THIN adapter: every request runs through `service.core`, and the only
work done here is renaming the native snake_case result into Harriet's
vocabulary. New capability goes to `service.v1`, never here.
"""
