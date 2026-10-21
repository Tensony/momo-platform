// Autoscaling demonstration: ramps request rate until ECS target tracking adds tasks, holds, then
// ramps down so scale-in is visible. Run against the deployed API:
//   k6 run -e BASE_URL=https://<alb> -e TOKEN=$USER_TOKEN -e FROM=<wallet> -e TO=<wallet> load/k6-autoscale.js
// Relax the fraud velocity rule first so the test measures capacity, not fraud declines:
//   aws ssm put-parameter --name /momo/fraud/config --type String --overwrite --value '{"velocity_max_txns":1000000}'
// Evidence: scripts/autoscale_evidence.sh dumps the scaling activities and task counts.
import http from 'k6/http';
import { check } from 'k6';
import { uuidv4 } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';

export const options = {
  insecureSkipTLSVerify: true, // self-signed ALB certificate in the demo environment
  scenarios: {
    ramp: {
      executor: 'ramping-arrival-rate',
      startRate: 5, timeUnit: '1s', preAllocatedVUs: 50, maxVUs: 400,
      stages: [
        { target: 20, duration: '2m' },
        { target: 120, duration: '4m' },   // CPU > 55% => scale OUT
        { target: 120, duration: '6m' },
        { target: 5, duration: '3m' },     // scale IN after the 5 min cooldown
      ],
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.02'],        // 4xx business rejections are not failures (see below)
    http_req_duration: ['p(95)<1000'],     // the SLO
  },
};

const H = { 'Content-Type': 'application/json', Authorization: `Bearer ${__ENV.TOKEN}` };

export default function () {
  const r = http.post(`${__ENV.BASE_URL}/transfers`,
    JSON.stringify({ from_wallet: __ENV.FROM, to_wallet: __ENV.TO, amount: 1 }),
    { headers: { ...H, 'Idempotency-Key': uuidv4(), 'X-Correlation-ID': `load-${uuidv4()}` },
      responseCallback: http.expectedStatuses(201, 422) });
  check(r, { 'handled (201 or business 422)': (x) => x.status === 201 || x.status === 422 });
}
