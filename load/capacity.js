import http from 'k6/http';
import { check } from 'k6';

const RATE = Number(__ENV.RATE || 10);

export const options = {
  scenarios: {
    capacity: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: '1m',
      preAllocatedVUs: 50,
      maxVUs: 200,
    },
  },
  summaryTrendStats: ['avg', 'p(95)', 'max'],
};

export default function () {
  const res = http.get('http://web/work');
  check(res, { 'status is 200': (r) => r.status === 200 });
}
