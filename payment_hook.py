# -*- coding: utf-8 -*-
"""Record wesais API flows locally; emit a separate summary without body values."""

import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from mitmproxy import ctx, http, io


class PaymentHook:
    def load(self, loader):
        loader.add_option("payment_capture_dir", str, "",
                          "Directory for this payment capture session")

    def running(self):
        self.directory = Path(ctx.options.payment_capture_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "payment.mitm").touch()
        self.stop_task = asyncio.create_task(self._watch_stop())

    async def _watch_stop(self):
        while not (self.directory / "stop").exists():
            await asyncio.sleep(0.2)
        ctx.master.shutdown()

    @staticmethod
    def matches(flow):
        # Capture the entire API sequence: state-query endpoints may not contain
        # 'pay', and we need to see the request made after clicking the button.
        return flow.request.host == "api.wesais.com"

    @staticmethod
    def fields(request):
        try:
            body = request.get_text(strict=False) or ""
            content_type = request.headers.get("content-type", "").lower()
            if "json" in content_type:
                value = json.loads(body)
                return sorted(value) if isinstance(value, dict) else []
            if "x-www-form-urlencoded" in content_type:
                return sorted({k for k, _ in parse_qsl(body, keep_blank_values=True)})
        except (ValueError, TypeError):
            pass
        return []

    def request(self, flow: http.HTTPFlow):
        if self.matches(flow):
            self._summary(flow, "request")

    def response(self, flow: http.HTTPFlow):
        if self.matches(flow):
            self._save(flow)
            self._summary(flow, "response")

    def error(self, flow: http.HTTPFlow):
        if self.matches(flow):
            self._summary(flow, "connection_error")

    def _save(self, flow):
        # Close/flush after each flow so a forced shutdown does not lose a
        # completed response. This evidence is never replayed by this tool.
        with (self.directory / "payment.mitm").open("ab") as out:
            io.FlowWriter(out).add(flow)

    def _summary(self, flow, event):
        record = {
            "event": event,
            "flow_id": flow.id,
            "started_at": flow.request.timestamp_start,
            "method": flow.request.method,
            "host": flow.request.host,
            "path": urlsplit(flow.request.url).path,
            "query_fields": sorted(set(flow.request.query.keys())),
            "body_fields": self.fields(flow.request),
        }
        if event == "response":
            record["http_status"] = flow.response.status_code
            try:
                value = json.loads(flow.response.get_text(strict=False))
                if isinstance(value, dict):
                    record["response_fields"] = sorted(value)
                    code = value.get("code")
                    if isinstance(code, int) or (isinstance(code, str) and code.isdigit()):
                        record["business_code"] = code
            except (ValueError, TypeError):
                pass
        with (self.directory / "summary.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(record, ensure_ascii=False) + "\n")


addons = [PaymentHook()]
