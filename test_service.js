"use strict";

const { spawnSync } = require("node:child_process");

// 运行全部 *_contract.py：服务契约、HTTP 契约、领域契约与事故解除回归契约。
const result = spawnSync(
  "python3",
  ["-m", "unittest", "-v",
   "service_contract", "api_contract", "domain_contract",
   "incident_contract", "api_incident_contract"],
  { stdio: "inherit" }
);
if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
