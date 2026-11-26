import http from 'k6/http';
import { check } from 'k6';

const patterns = {
  smoke: [
    { duration: '30s', target: 5 },
  ],
  ramp: [
    { duration: '1m', target: 5 },
    { duration: '5m', target: 40 },
    { duration: '2m', target: 40 },
    { duration: '2m', target: 5 },
  ],
  spike: [
    { duration: '1m', target: 5 },
    { duration: '10s', target: 40 },
    { duration: '3m', target: 40 },
    { duration: '10s', target: 5 },
    { duration: '3m', target: 5 },
  ],
  periodic: [
    { duration: '1m', target: 5 },
    { duration: '1m', target: 35 },
    { duration: '1m', target: 35 },
    { duration: '1m', target: 5 },
    { duration: '1m', target: 5 },
    { duration: '1m', target: 35 },
    { duration: '1m', target: 35 },
    { duration: '1m', target: 5 },
  ],
};

const PATTERN = __ENV.PATTERN || 'smoke';

export const options = {
  scenarios: {
    load: {
      executor: 'ramping-arrival-rate',
      startRate: 5,
      timeUnit: '1s',
      preAllocatedVUs: 50,
      maxVUs: 200,
      stages: patterns[PATTERN],
    },
  },
  summaryTrendStats: ['avg', 'p(95)', 'max'],
};

export default function () {
  const res = http.get('http://web/work');
  check(res, { 'status is 200': (r) => r.status === 200 });
}
