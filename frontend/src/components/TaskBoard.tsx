import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  DndContext,
  PointerSensor,
  TouchSensor,
  useSensor,
  useSensors,
  DragOverlay,
  useDroppable,
  useDraggable,
} from '@dnd-kit/core'
import type { DragEndEvent, DragStartEvent } from '@dnd-kit/core'
import { Plus, Loader2 } from 'lucide-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { getTasks, createTask, updateTask, deleteTask } from '../api/client'
import { TaskCard } from './TaskCard'
import type { Task, UserID } from '../api/client'
import { useToast } from '../hooks/useToast'

type ColId = 'pending' | 'urgent' | 'done'

const columns: { id: ColId; label: string; color: string }[] = [
  { id: 'pending', label: 'Pending',  color: '#00D4FF' },
  { id: 'urgent',  label: 'Urgent',   color: '#FF4466' },
  { id: 'done',    label: 'Done',     color: '#00FF88' },
]

function taskToCol(task: Task): ColId {
  if (task.status === 'done') return 'done'
  if (task.priority === 'urgent') return 'urgent'
  return 'pending'
}

function DraggableCard({ task, onDelete, onUpdate }: { task: Task; onDelete: (id: string) => void; onUpdate: (id: string, d: Partial<Task>) => void }) {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({ id: task.id })
  return (
    <div ref={setNodeRef} {...attributes} {...listeners} style={{ touchAction: 'none' }}>
      <TaskCard task={task} onDelete={onDelete} onUpdate={onUpdate} dragging={isDragging} />
    </div>
  )
}

function DroppableCol({ id, label, color, children }: { id: ColId; label: string; color: string; children: React.ReactNode }) {
  const { setNodeRef, isOver } = useDroppable({ id })
  return (
    <div
      ref={setNodeRef}
      className="flex-1 min-h-[200px] rounded-2xl p-3 transition-all duration-200"
      style={{
        background: isOver ? `${color}08` : 'rgba(13,22,40,0.3)',
        border: `1px solid ${isOver ? color + '30' : 'rgba(30,58,95,0.3)'}`,
      }}
    >
      <div className="flex items-center gap-2 mb-3 px-1">
        <div className="w-2 h-2 rounded-full" style={{ background: color }} />
        <span className="text-xs font-semibold uppercase tracking-wider" style={{ color }}>
          {label}
        </span>
      </div>
      <div className="space-y-2">{children}</div>
    </div>
  )
}

interface TaskBoardProps {
  userId: UserID
}

export function TaskBoard({ userId }: TaskBoardProps) {
  const qc = useQueryClient()
  const { addToast } = useToast()
  const [activeTask, setActiveTask] = useState<Task | null>(null)
  const [newTitle, setNewTitle] = useState('')
  const [adding, setAdding] = useState(false)

  const { data: tasks = [], isLoading } = useQuery({
    queryKey: ['tasks', userId],
    queryFn: () => getTasks(userId).then(r => r.data),
  })

  const createMut = useMutation({
    mutationFn: (title: string) => createTask({ title, priority: 'medium', user_id: userId }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['tasks', userId] }); setNewTitle('') },
    onError: () => addToast('Failed to create task', 'error'),
  })

  const updateMut = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Partial<Task> }) =>
      updateTask(id, data, userId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tasks', userId] }),
    onError: () => addToast('Update failed', 'error'),
  })

  const deleteMut = useMutation({
    mutationFn: (id: string) => deleteTask(id, userId),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['tasks', userId] }); addToast('Task deleted', 'info') },
  })

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 200, tolerance: 6 } })
  )

  const handleDragStart = (e: DragStartEvent) => {
    const task = tasks.find(t => t.id === e.active.id)
    if (task) setActiveTask(task)
  }

  const handleDragEnd = (e: DragEndEvent) => {
    setActiveTask(null)
    const { active, over } = e
    if (!over || active.id === over.id) return

    const task = tasks.find(t => t.id === active.id)
    if (!task) return

    const destCol = over.id as ColId
    let updates: Partial<Task> = {}

    if (destCol === 'done') updates = { status: 'done' }
    else if (destCol === 'urgent') updates = { status: 'pending', priority: 'urgent' }
    else updates = { status: 'pending', priority: task.priority === 'urgent' ? 'high' : task.priority }

    updateMut.mutate({ id: task.id, data: updates })
  }

  const colTasks = (colId: ColId) => tasks.filter(t => taskToCol(t) === colId)

  if (isLoading) {
    return (
      <div className="flex gap-3">
        {columns.map(c => (
          <div key={c.id} className="flex-1 glass rounded-2xl p-4 space-y-2 min-h-[200px]">
            <div className="skeleton h-3 w-16 rounded bg-[#1E3A5F]/40 mb-3" />
            {[1,2].map(i => <div key={i} className="skeleton h-12 rounded-xl bg-[#1E3A5F]/30" />)}
          </div>
        ))}
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <DndContext sensors={sensors} onDragStart={handleDragStart} onDragEnd={handleDragEnd}>
        <div className="flex gap-3 overflow-x-auto pb-2">
          {columns.map(col => (
            <DroppableCol key={col.id} id={col.id} label={col.label} color={col.color}>
              <AnimatePresence>
                {colTasks(col.id).map(task => (
                  <motion.div
                    key={task.id}
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, scale: 0.95 }}
                    transition={{ duration: 0.2 }}
                  >
                    <DraggableCard
                      task={task}
                      onDelete={(id) => deleteMut.mutate(id)}
                      onUpdate={(id, data) => updateMut.mutate({ id, data })}
                    />
                  </motion.div>
                ))}
              </AnimatePresence>

              {colTasks(col.id).length === 0 && (
                <div className="text-center py-6 text-[#4A6080] text-xs">
                  Drop tasks here
                </div>
              )}
            </DroppableCol>
          ))}
        </div>

        <DragOverlay>
          {activeTask && <TaskCard task={activeTask} dragging />}
        </DragOverlay>
      </DndContext>

      {/* Add task */}
      <AnimatePresence>
        {adding ? (
          <motion.form
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            onSubmit={(e) => { e.preventDefault(); if (newTitle.trim()) createMut.mutate(newTitle.trim()); setAdding(false) }}
            className="flex gap-2"
          >
            <input
              autoFocus
              value={newTitle}
              onChange={e => setNewTitle(e.target.value)}
              onBlur={() => { if (!newTitle.trim()) setAdding(false) }}
              onKeyDown={e => e.key === 'Escape' && setAdding(false)}
              placeholder="New task title…"
              className="flex-1 glass-sm px-3 py-2 rounded-xl text-sm text-[#E2E8F0]
                         placeholder:text-[#4A6080] outline-none focus:border-[#00D4FF]/40
                         bg-transparent border border-[#1E3A5F]/40"
            />
            <button
              type="submit"
              disabled={createMut.isPending || !newTitle.trim()}
              className="px-4 py-2 rounded-xl text-sm font-medium bg-[#00D4FF]/15
                         border border-[#00D4FF]/30 text-[#00D4FF] hover:bg-[#00D4FF]/20
                         transition-colors disabled:opacity-40"
            >
              {createMut.isPending ? <Loader2 size={14} className="animate-spin" /> : 'Add'}
            </button>
          </motion.form>
        ) : (
          <motion.button
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            onClick={() => setAdding(true)}
            className="flex items-center gap-2 text-sm text-[#4A6080] hover:text-[#00D4FF]
                       transition-colors px-2 py-1.5"
          >
            <Plus size={15} />
            Add task
          </motion.button>
        )}
      </AnimatePresence>
    </div>
  )
}
