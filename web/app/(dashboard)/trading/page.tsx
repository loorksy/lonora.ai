'use client'

import { useState, useEffect } from 'react'
import toast from 'react-hot-toast'
import {
  TrendingUp,
  Clock,
  CheckCircle,
  XCircle,
  AlertCircle,
  Link2,
  Copy,
  X,
  ShieldAlert
} from 'lucide-react'
import { apiClient } from '@/lib/api/client'
import type { TradeProposal } from '@/lib/api/trading'

const STATUS_STYLE: Record<string, string> = {
  proposed: 'bg-gray-100 text-gray-700',
  pending_approval: 'bg-amber-100 text-amber-700',
  approved: 'bg-blue-100 text-blue-700',
  rejected: 'bg-gray-100 text-gray-600',
  expired: 'bg-gray-100 text-gray-600',
  executing: 'bg-purple-100 text-purple-700',
  executed: 'bg-emerald-100 text-emerald-700',
  failed: 'bg-red-100 text-red-700',
}

const STATUS_ICON: Record<string, any> = {
  pending_approval: Clock,
  approved: Clock,
  executing: Clock,
  executed: CheckCircle,
  rejected: XCircle,
  expired: XCircle,
  failed: AlertCircle,
}

export default function TradingPage() {
  const [proposals, setProposals] = useState<TradeProposal[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState('pending_approval')
  const [actingOn, setActingOn] = useState<string | null>(null)
  const [showLinkModal, setShowLinkModal] = useState(false)
  const [linkCode, setLinkCode] = useState<string | null>(null)
  const [generatingCode, setGeneratingCode] = useState(false)

  useEffect(() => {
    fetchProposals()
  }, [statusFilter])

  const fetchProposals = async () => {
    try {
      setLoading(true)
      const data = await apiClient.getTradeProposals(statusFilter === 'all' ? undefined : statusFilter)
      setProposals(Array.isArray(data) ? data : [])
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An error occurred')
      toast.error('Failed to load trade proposals')
    } finally {
      setLoading(false)
    }
  }

  const handleApprove = async (proposal: TradeProposal) => {
    setActingOn(proposal.id)
    try {
      await apiClient.approveTradeProposal(proposal.id)
      toast.success(`Approved ${proposal.symbol} ${proposal.action} — execution in progress`)
      fetchProposals()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to approve proposal')
    } finally {
      setActingOn(null)
    }
  }

  const handleReject = async (proposal: TradeProposal) => {
    setActingOn(proposal.id)
    try {
      await apiClient.rejectTradeProposal(proposal.id)
      toast.success(`Rejected ${proposal.symbol} ${proposal.action}`)
      fetchProposals()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to reject proposal')
    } finally {
      setActingOn(null)
    }
  }

  const handleGenerateLinkCode = async () => {
    setGeneratingCode(true)
    try {
      const result = await apiClient.generateChannelLinkCode()
      setLinkCode(result.code)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to generate link code')
    } finally {
      setGeneratingCode(false)
    }
  }

  const copyCode = () => {
    if (!linkCode) return
    navigator.clipboard.writeText(linkCode)
    toast.success('Code copied')
  }

  const pendingCount = proposals.filter(p => p.status === 'pending_approval').length

  if (loading) {
    return (
      <div className="dashboard-resource-page flex h-screen items-center justify-center">
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mx-auto mb-4"></div>
          <p className="text-gray-600">Loading trade proposals...</p>
        </div>
      </div>
    )
  }

  return (
    <div className="dashboard-resource-page min-h-screen p-4 md:p-6">
      <div className="max-w-6xl mx-auto">
        {/* Header */}
        <div className="mb-8">
          <div className="flex items-center justify-between mb-6 flex-wrap gap-4">
            <div>
              <h1 className="text-2xl md:text-3xl font-extrabold text-gray-900 tracking-tight">Trading</h1>
              <p className="text-gray-600 mt-1">
                Review and approve trade proposals from the Trading Agent. Nothing executes without your approval.
              </p>
            </div>
            <button
              onClick={() => {
                setShowLinkModal(true)
                setLinkCode(null)
              }}
              className="inline-flex items-center gap-2 px-4 py-2.5 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
            >
              <Link2 className="w-4 h-4" />
              Link Telegram / WhatsApp
            </button>
          </div>

          {/* Stats */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
            <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-5">
              <div className="flex items-center gap-3">
                <div className="p-2.5 bg-blue-100 rounded-xl">
                  <TrendingUp className="w-5 h-5 text-blue-600" />
                </div>
                <div>
                  <p className="text-sm text-gray-600">Total (this view)</p>
                  <p className="text-2xl font-bold text-gray-900">{proposals.length}</p>
                </div>
              </div>
            </div>

            <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-5">
              <div className="flex items-center gap-3">
                <div className="p-2.5 bg-amber-100 rounded-xl">
                  <Clock className="w-5 h-5 text-amber-600" />
                </div>
                <div>
                  <p className="text-sm text-gray-600">Awaiting Approval</p>
                  <p className="text-2xl font-bold text-gray-900">{pendingCount}</p>
                </div>
              </div>
            </div>

            <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-5">
              <div className="flex items-center gap-3">
                <div className="p-2.5 bg-gray-100 rounded-xl">
                  <ShieldAlert className="w-5 h-5 text-gray-600" />
                </div>
                <div>
                  <p className="text-sm text-gray-600">Approval requires</p>
                  <p className="text-sm font-semibold text-gray-900">trading:approve permission</p>
                </div>
              </div>
            </div>
          </div>

          {/* Filter */}
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="px-4 py-2.5 bg-white border border-gray-200 rounded-xl focus:ring-2 focus:ring-blue-500 focus:border-transparent shadow-sm"
          >
            <option value="pending_approval">Awaiting Approval</option>
            <option value="all">All Statuses</option>
            <option value="approved">Approved</option>
            <option value="executed">Executed</option>
            <option value="rejected">Rejected</option>
            <option value="expired">Expired</option>
            <option value="failed">Failed</option>
          </select>
        </div>

        {/* Error */}
        {error && (
          <div className="mb-6 bg-red-50 border border-red-200 rounded-xl p-4">
            <div className="flex items-center gap-3">
              <AlertCircle className="w-5 h-5 text-red-600" />
              <p className="text-red-700">{error}</p>
            </div>
          </div>
        )}

        {/* Proposals List */}
        {proposals.length === 0 ? (
          <div className="bg-white rounded-2xl shadow-sm border border-gray-100 p-12 text-center">
            <div className="w-32 h-32 mx-auto mb-6 relative">
              <div className="absolute inset-0 bg-gradient-to-br from-emerald-100 to-emerald-50 rounded-2xl transform rotate-6"></div>
              <div className="absolute inset-0 bg-white rounded-2xl shadow-sm border border-gray-100 flex items-center justify-center">
                <CheckCircle className="w-12 h-12 text-emerald-500" />
              </div>
            </div>
            <h3 className="text-xl font-semibold text-gray-900 mb-2">Nothing here</h3>
            <p className="text-gray-600 max-w-md mx-auto">
              No trade proposals in this view. New proposals from the Trading Agent will show up here.
            </p>
          </div>
        ) : (
          <div className="space-y-4">
            {proposals.map((proposal) => {
              const Icon = STATUS_ICON[proposal.status] || Clock
              return (
                <div
                  key={proposal.id}
                  className="bg-white rounded-xl shadow-sm border border-gray-100 hover:border-blue-200 transition-all hover:shadow-md"
                >
                  <div className="p-5">
                    <div className="flex items-start justify-between gap-4 mb-3">
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          <span
                            className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${
                              STATUS_STYLE[proposal.status] || 'bg-gray-100 text-gray-600'
                            }`}
                          >
                            <Icon className="w-3.5 h-3.5" />
                            {proposal.status.replace(/_/g, ' ')}
                          </span>
                          <span className="text-xs text-gray-500 uppercase tracking-wide">{proposal.symbol}</span>
                        </div>
                        <h3 className="text-lg font-semibold text-gray-900">
                          {proposal.action?.toUpperCase()} {proposal.volume || ''} {proposal.symbol}
                        </h3>
                        <p className="text-sm text-gray-600 mt-1">
                          {proposal.stop_loss && <span>SL: {proposal.stop_loss} </span>}
                          {proposal.take_profit && <span>TP: {proposal.take_profit}</span>}
                        </p>
                        {proposal.failure_reason && (
                          <p className="text-sm text-red-600 mt-1">{proposal.failure_reason}</p>
                        )}
                      </div>
                      <span className="text-xs text-gray-500 whitespace-nowrap">
                        {new Date(proposal.created_at).toLocaleString()}
                      </span>
                    </div>

                    {proposal.status === 'pending_approval' && (
                      <div className="flex gap-2 justify-end">
                        <button
                          onClick={() => handleReject(proposal)}
                          disabled={actingOn === proposal.id}
                          className="inline-flex items-center gap-2 px-3 py-2 text-sm font-medium text-gray-700 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors disabled:opacity-50"
                        >
                          <XCircle className="w-4 h-4" />
                          Reject
                        </button>
                        <button
                          onClick={() => handleApprove(proposal)}
                          disabled={actingOn === proposal.id}
                          className="inline-flex items-center gap-2 px-3 py-2 text-sm font-medium text-white bg-emerald-600 rounded-lg hover:bg-emerald-700 transition-colors disabled:opacity-50"
                        >
                          {actingOn === proposal.id ? (
                            <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                          ) : (
                            <CheckCircle className="w-4 h-4" />
                          )}
                          Approve
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* Link Telegram/WhatsApp Modal */}
      {showLinkModal && (
        <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center p-4 z-50">
          <div className="bg-white rounded-2xl shadow-2xl max-w-md w-full">
            <div className="border-b border-gray-100 px-6 py-4 flex items-center justify-between">
              <h3 className="text-lg font-semibold text-gray-900">Link Telegram / WhatsApp</h3>
              <button
                onClick={() => setShowLinkModal(false)}
                className="p-2 hover:bg-gray-100 rounded-lg transition-colors"
              >
                <X className="w-5 h-5 text-gray-500" />
              </button>
            </div>

            <div className="p-6">
              <p className="text-sm text-gray-600 mb-4">
                Generate a one-time code, then send it to the Trading Agent&apos;s bot to approve/reject trade
                proposals from that channel. Valid for 10 minutes, single-use.
              </p>
              <p className="text-sm text-gray-600 mb-4">
                Telegram: send <code className="bg-gray-100 px-1.5 py-0.5 rounded">/link &lt;code&gt;</code> to the
                bot. WhatsApp: send <code className="bg-gray-100 px-1.5 py-0.5 rounded">LINK &lt;code&gt;</code>.
              </p>

              {linkCode ? (
                <div className="flex items-center gap-2 mb-4">
                  <code className="flex-1 text-center text-xl font-mono font-bold tracking-widest bg-gray-50 border border-gray-200 rounded-lg py-3">
                    {linkCode}
                  </code>
                  <button
                    onClick={copyCode}
                    className="p-3 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors"
                    title="Copy code"
                  >
                    <Copy className="w-4 h-4 text-gray-700" />
                  </button>
                </div>
              ) : (
                <button
                  onClick={handleGenerateLinkCode}
                  disabled={generatingCode}
                  className="w-full inline-flex items-center justify-center gap-2 px-4 py-3 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50"
                >
                  {generatingCode ? (
                    <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                  ) : (
                    <Link2 className="w-4 h-4" />
                  )}
                  Generate Code
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
