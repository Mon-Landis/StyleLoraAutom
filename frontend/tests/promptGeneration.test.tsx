import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { GenerateDialog } from '../src/features/prompts/GenerateDialog';
import { enqueueGeneration } from '../src/api/promptGroups';

vi.mock('../src/api/promptGroups', () => ({ enqueueGeneration: vi.fn() }));

describe('theme generation', () => {
  it('requires a theme and 1–200 integer entries, submits to the selected group', async () => {
    vi.mocked(enqueueGeneration).mockResolvedValue({ task_id: 'task' });
    const onSubmitted = vi.fn();
    const onClose = vi.fn();
    const ref = { scope: 'global' as const, project_token: null, group_id: 'group' };
    render(<GenerateDialog gref={ref} groupName="Empty group" onClose={onClose} onSubmitted={onSubmitted} />);
    const button = screen.getByRole('button', { name: '生成' });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('主题'), { target: { value: 'Forest' } });
    fireEvent.change(screen.getByLabelText('条数'), { target: { value: '201' } });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText('条数'), { target: { value: '45' } });
    fireEvent.click(button);
    await waitFor(() => expect(onSubmitted).toHaveBeenCalledWith('task'));
    expect(enqueueGeneration).toHaveBeenCalledWith(ref, 'Forest', 45, expect.any(String));
    expect(onClose).toHaveBeenCalled();
  });
});
