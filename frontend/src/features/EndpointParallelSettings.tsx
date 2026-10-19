import { useId, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { listLlmEndpoints, saveEndpointParallel, type LlmEndpoint } from '../api/llmEndpoints';
import { Button, Panel } from '../ui/primitives';
import { ErrorView } from '../ui/ErrorView';
import { Empty, Loading } from '../ui/slots';

function EndpointParallelForm({ endpoint }: { endpoint: LlmEndpoint }) {
  const id = useId();
  const qc = useQueryClient();
  const [value, setValue] = useState(String(endpoint.max_parallel ?? 4));
  const limit = Number(value);
  const valid = value.trim() !== '' && Number.isInteger(limit) && (limit === -1 || limit >= 1);
  const mutation = useMutation({
    mutationFn: () => saveEndpointParallel(endpoint, limit),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['llm-endpoints'] }),
  });
  return (
    <form onSubmit={(event) => { event.preventDefault(); if (valid) mutation.mutate(); }}>
      <p>{endpoint.base_url} · {endpoint.protocol}</p>
      <div className="form-row">
        <div className="field">
          <label className="field__label" htmlFor={id}>最大并行</label>
          <input id={id} type="number" min={-1} step={1} required value={value}
            aria-describedby={`${id}-help`} aria-invalid={!valid}
            onChange={(event) => setValue(event.target.value)} />
          <span id={`${id}-help`}>默认 4；-1 为不限制，其余须为大于等于 1 的整数。每个端点独立设置。</span>
        </div>
        <Button type="submit" disabled={!valid || mutation.isPending}>保存</Button>
      </div>
      {mutation.error && <ErrorView error={mutation.error} feature="端点最大并行" />}
      {mutation.isSuccess && <p role="status">已保存</p>}
    </form>
  );
}

export function EndpointParallelSettings() {
  const query = useQuery({ queryKey: ['llm-endpoints'], queryFn: listLlmEndpoints });
  return (
    <Panel title="LLM 发送端点">
      {query.isLoading && <Loading />}
      {query.error && <ErrorView error={query.error} feature="LLM 发送端点" />}
      {query.data?.length === 0 && <Empty title="暂无 LLM 端点" />}
      {query.data?.map((endpoint) => <EndpointParallelForm key={`${endpoint.endpoint_id}:${endpoint.version}`} endpoint={endpoint} />)}
    </Panel>
  );
}
