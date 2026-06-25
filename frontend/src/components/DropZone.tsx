import { useState, useRef, useCallback } from 'react'
import { generateId } from '../utils/uuid'
import { motion, AnimatePresence } from 'framer-motion'
import { Upload, FileText, CheckCircle, XCircle, X, Loader2 } from 'lucide-react'
import axios from 'axios'
import type { UserID } from '../api/client'
import { useToast } from '../hooks/useToast'

interface UploadedFile {
  id: string
  name: string
  size: number
  status: 'uploading' | 'indexed' | 'failed'
  progress: number
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`
}

interface DropZoneProps {
  userId: UserID
}

export function DropZone({ userId }: DropZoneProps) {
  const [dragging, setDragging] = useState(false)
  const [files, setFiles] = useState<UploadedFile[]>([])
  const inputRef = useRef<HTMLInputElement>(null)
  const { addToast } = useToast()

  const uploadFile = useCallback(async (file: File) => {
    const id = generateId()
    setFiles(prev => [...prev, { id, name: file.name, size: file.size, status: 'uploading', progress: 0 }])

    const form = new FormData()
    form.append('file', file)
    form.append('user_id', userId)

    try {
      await axios.post('/api/ingest/upload', form, {
        onUploadProgress: (e) => {
          const pct = Math.round((e.loaded / (e.total ?? file.size)) * 100)
          setFiles(prev => prev.map(f => f.id === id ? { ...f, progress: pct } : f))
        }
      })
      setFiles(prev => prev.map(f => f.id === id ? { ...f, status: 'indexed', progress: 100 } : f))
      addToast(`${file.name} indexed`, 'success')
    } catch {
      setFiles(prev => prev.map(f => f.id === id ? { ...f, status: 'failed' } : f))
      addToast(`Failed to upload ${file.name}`, 'error')
    }
  }, [userId, addToast])

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setDragging(false)
    Array.from(e.dataTransfer.files).forEach(uploadFile)
  }

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) Array.from(e.target.files).forEach(uploadFile)
  }

  const removeFile = (id: string) => setFiles(prev => prev.filter(f => f.id !== id))

  return (
    <div className="space-y-4">
      {/* Drop zone */}
      <motion.div
        onDragEnter={() => setDragging(true)}
        onDragLeave={() => setDragging(false)}
        onDragOver={e => e.preventDefault()}
        onDrop={handleDrop}
        onClick={() => inputRef.current?.click()}
        animate={{
          borderColor: dragging ? 'rgba(0,212,255,0.6)' : 'rgba(30,58,95,0.4)',
          background: dragging ? 'rgba(0,212,255,0.05)' : 'rgba(13,22,40,0.3)',
          boxShadow: dragging ? '0 0 30px rgba(0,212,255,0.1)' : 'none',
        }}
        className="relative rounded-2xl border-2 border-dashed p-12 text-center cursor-pointer
                   transition-colors duration-200 select-none"
      >
        <motion.div
          animate={{ y: dragging ? -4 : 0 }}
          transition={{ type: 'spring', stiffness: 300 }}
        >
          <Upload
            size={36}
            className="mx-auto mb-3"
            style={{ color: dragging ? '#00D4FF' : '#1E3A5F' }}
          />
          <p className="text-[#E2E8F0] font-medium mb-1">
            {dragging ? 'Drop to upload' : 'Drag files here to index'}
          </p>
          <p className="text-[#4A6080] text-sm">PDF, DOCX, TXT — added to Aria's knowledge base</p>
          <p className="text-xs text-[#1E3A5F] mt-2">or click to browse</p>
        </motion.div>

        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".pdf,.docx,.doc,.txt,.md"
          onChange={handleChange}
          className="hidden"
        />
      </motion.div>

      {/* File list */}
      <AnimatePresence>
        {files.map(file => (
          <motion.div
            key={file.id}
            initial={{ opacity: 0, y: -8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, x: 20, scale: 0.96 }}
            className="glass-sm rounded-xl p-3 flex items-center gap-3"
          >
            <FileText size={16} className="shrink-0 text-[#4A6080]" />

            <div className="flex-1 min-w-0">
              <div className="flex items-center justify-between gap-2 mb-1">
                <span className="text-sm text-[#E2E8F0] truncate">{file.name}</span>
                <span className="text-xs text-[#4A6080] shrink-0">{formatBytes(file.size)}</span>
              </div>

              {file.status === 'uploading' && (
                <div className="w-full h-1 bg-[#1E3A5F]/50 rounded-full overflow-hidden">
                  <motion.div
                    className="h-full rounded-full bg-[#00D4FF]"
                    animate={{ width: `${file.progress}%` }}
                    transition={{ duration: 0.3 }}
                  />
                </div>
              )}

              {file.status === 'indexed' && (
                <span className="text-xs text-[#00FF88]">Indexed</span>
              )}

              {file.status === 'failed' && (
                <span className="text-xs text-[#FF4466]">Upload failed</span>
              )}
            </div>

            {/* Status icon */}
            <div className="shrink-0">
              {file.status === 'uploading' && (
                <Loader2 size={16} className="text-[#00D4FF] animate-spin" />
              )}
              {file.status === 'indexed' && (
                <CheckCircle size={16} className="text-[#00FF88]" />
              )}
              {file.status === 'failed' && (
                <XCircle size={16} className="text-[#FF4466]" />
              )}
            </div>

            <button
              onClick={() => removeFile(file.id)}
              className="shrink-0 text-[#4A6080] hover:text-[#E2E8F0] transition-colors"
            >
              <X size={14} />
            </button>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  )
}
