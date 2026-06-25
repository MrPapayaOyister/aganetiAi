import { useState } from 'react'
import { motion } from 'framer-motion'
import { Search, FolderOpen } from 'lucide-react'
import { DropZone } from '../components/DropZone'
import { useAppContext } from '../App'

export default function FilesPage() {
  const { userId } = useAppContext()
  const [searchQuery, setSearchQuery] = useState('')

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-3xl mx-auto px-4 py-6 space-y-6 pb-24 md:pb-6">
        {/* Header */}
        <motion.div initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }}>
          <div className="flex items-center gap-2 mb-1">
            <FolderOpen size={18} className="text-[#00D4FF]" />
            <h1 className="text-lg font-semibold text-[#E2E8F0]">Knowledge Files</h1>
          </div>
          <p className="text-sm text-[#4A6080]">
            Upload documents to add them to Aria's searchable knowledge base.
          </p>
        </motion.div>

        {/* Search */}
        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.1 }}
          className="glass-sm rounded-xl flex items-center gap-3 px-4 py-3"
        >
          <Search size={16} className="text-[#4A6080] shrink-0" />
          <input
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            placeholder="Search indexed knowledge…"
            className="flex-1 bg-transparent outline-none text-sm text-[#E2E8F0]
                       placeholder:text-[#4A6080]"
          />
        </motion.div>

        {searchQuery.trim() && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="glass rounded-2xl p-6 text-center"
          >
            <p className="text-[#4A6080] text-sm">
              Semantic search coming soon — ask Aria directly: "What does our handbook say about{' '}
              <em>{searchQuery}</em>?"
            </p>
          </motion.div>
        )}

        {/* Drop zone */}
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.2 }}
        >
          <DropZone userId={userId} />
        </motion.div>

        {/* Tips */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.4 }}
          className="glass rounded-2xl p-4"
        >
          <h3 className="text-sm font-semibold text-[#94A3B8] mb-3">Supported formats</h3>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            {['PDF', 'DOCX', 'TXT', 'MD'].map(fmt => (
              <div
                key={fmt}
                className="text-center py-2 px-3 rounded-xl bg-[#1E3A5F]/20 border border-[#1E3A5F]/30"
              >
                <span className="text-xs font-mono text-[#00D4FF]">.{fmt.toLowerCase()}</span>
              </div>
            ))}
          </div>
          <p className="text-xs text-[#4A6080] mt-3">
            Files are chunked, embedded via fastembed, and stored in Qdrant for semantic retrieval.
          </p>
        </motion.div>
      </div>
    </div>
  )
}
