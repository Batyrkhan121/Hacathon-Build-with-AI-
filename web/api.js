// Обмен с бэкендом строго по контракту app/schemas.py:
//   POST api/recommend  RecommendRequest -> Answer
//   GET  api/landmarks  -> list[Landmark]
// В теле запроса никогда нет координат: только location.h3_cell или location.landmark_id.
// Режим мока: ?mock=1 (ещё ?mock=empty, ?mock=error, ?mock=slow для проверки состояний).

export const TIMEOUT_MS = 25000;

const params = new URLSearchParams(location.search);
const mockParam = params.get('mock');
export const MOCK = mockParam !== null && mockParam !== '0' && mockParam !== 'false' ? mockParam : null;
export const isMock = MOCK !== null;

export class ApiError extends Error {
  constructor(kind, { status = null, detail = '' } = {}) {
    super(`${kind}${status ? ` ${status}` : ''}${detail ? `: ${detail}` : ''}`);
    this.name = 'ApiError';
    // timeout | network | validation | too_large | unavailable | server | format | mock | aborted
    this.kind = kind;
    this.status = status;
    this.detail = detail;
  }
}

async function getJson(url, signal) {
  const res = await fetch(url, { signal, headers: { Accept: 'application/json' } });
  if (!res.ok) throw new ApiError('server', { status: res.status });
  return res.json();
}

export async function fetchLandmarks() {
  // http.server из корня отдаёт страницу как /web/ — там API нет, файл ориентиров рядом.
  const devServer = location.pathname.includes('/web/');
  const urls =
    isMock && devServer ? ['../data/landmarks.json', 'api/landmarks'] : ['api/landmarks', '../data/landmarks.json'];
  for (const url of urls) {
    try {
      const data = await getJson(url);
      if (Array.isArray(data) && data.length) return data;
    } catch {
      // пробуем следующий источник
    }
  }
  throw new ApiError('network', { detail: 'landmarks' });
}

export async function fetchMeta() {
  if (isMock) return null;
  try {
    return await getJson('api/meta');
  } catch {
    return null;
  }
}

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    const id = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => {
      clearTimeout(id);
      reject(signal.reason);
    }, { once: true });
  });
}

// Таймаут 25 с + отмена извне (новый запрос отменяет старый).
function withTimeout(outer) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(new ApiError('timeout')), TIMEOUT_MS);
  const onOuter = () => ctrl.abort(new ApiError('aborted'));
  if (outer) {
    if (outer.aborted) onOuter();
    else outer.addEventListener('abort', onOuter, { once: true });
  }
  return {
    signal: ctrl.signal,
    done() {
      clearTimeout(timer);
      outer?.removeEventListener('abort', onOuter);
    },
  };
}

function toApiError(err, signal) {
  if (err instanceof ApiError) return err;
  if (signal?.aborted && signal.reason instanceof ApiError) return signal.reason;
  if (err?.name === 'AbortError') return new ApiError('aborted');
  return new ApiError('network', { detail: err?.message || '' });
}

function validationDetail(body) {
  const d = body?.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d) && d.length) {
    return d
      .slice(0, 2)
      .map((e) => `${Array.isArray(e.loc) ? e.loc.slice(1).join('.') : ''} ${e.msg || ''}`.trim())
      .join('; ');
  }
  return '';
}

function looksLikeAnswer(a) {
  return a && typeof a === 'object' && Array.isArray(a.cards) && typeof a.summary === 'string' && a.data_used;
}

async function mockRecommend(body, signal) {
  await sleep(MOCK === 'slow' ? 3000 : 650, signal);
  if (MOCK === 'error' && body.use_llm) throw new ApiError('mock');
  const answer = await getJson('mock/answer_sample.json', signal);
  if (MOCK === 'empty') {
    answer.cards = [];
    answer.route = null;
    answer.summary = 'МОК: рядом ничего не подошло под запрос.';
  }
  return answer;
}

export async function recommend(body, outerSignal) {
  const t = withTimeout(outerSignal);
  try {
    let answer;
    if (isMock) {
      answer = await mockRecommend(body, t.signal);
    } else {
      const res = await fetch('api/recommend', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(body),
        signal: t.signal,
      });
      if (res.status === 422) {
        const err = await res.json().catch(() => null);
        throw new ApiError('validation', { status: 422, detail: validationDetail(err) });
      }
      // 413: тело больше лимита ApiGuard (16 КБ) или прокси; повтор без ИИ тут не поможет.
      if (res.status === 413) throw new ApiError('too_large', { status: 413 });
      // 503: сервер жив, но данные ещё не загружены.
      if (res.status === 503) throw new ApiError('unavailable', { status: 503 });
      if (!res.ok) throw new ApiError('server', { status: res.status });
      answer = await res.json().catch(() => {
        throw new ApiError('format');
      });
    }
    if (!looksLikeAnswer(answer)) throw new ApiError('format');
    return answer;
  } catch (err) {
    throw toApiError(err, t.signal);
  } finally {
    t.done();
  }
}
