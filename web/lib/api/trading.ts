import { apiClient } from './http'

export interface TradeProposal {
  id: string
  status: string
  symbol: string
  action: string
  volume: string | null
  stop_loss: string | null
  take_profit: string | null
  expires_at: string | null
  failure_reason: string | null
  created_at: string
  [key: string]: unknown
}

export async function getTradeProposals(status?: string): Promise<TradeProposal[]> {
  const params = status ? { status } : {}
  const { data } = await apiClient.axios.get('/api/v1/trading/proposals', { params })
  return data.data || []
}

export async function getTradeProposal(proposalId: string): Promise<TradeProposal> {
  const { data } = await apiClient.axios.get(`/api/v1/trading/proposals/${proposalId}`)
  return data.data
}

export async function approveTradeProposal(proposalId: string): Promise<TradeProposal> {
  const { data } = await apiClient.axios.post(`/api/v1/trading/proposals/${proposalId}/approve`)
  return data.data
}

export async function rejectTradeProposal(proposalId: string, reason?: string): Promise<TradeProposal> {
  const { data } = await apiClient.axios.post(`/api/v1/trading/proposals/${proposalId}/reject`, { reason })
  return data.data
}

export async function generateChannelLinkCode(): Promise<{ code: string; expires_in_seconds: number }> {
  const { data } = await apiClient.axios.post('/api/v1/trading/channel-link/generate')
  return data.data
}

export async function getTradingAccounts(): Promise<any[]> {
  const { data } = await apiClient.axios.get('/api/v1/trading/accounts')
  return data.data || []
}
