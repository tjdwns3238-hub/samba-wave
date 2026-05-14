'use client'

import React, { Dispatch, SetStateAction } from 'react'
import { orderApi } from '@/lib/samba/api/commerce'
import { showAlert } from '@/components/samba/Modal'

interface Props {
  open: boolean
  onClose: () => void
  alarmHour: string
  setAlarmHour: Dispatch<SetStateAction<string>>
  alarmMin: string
  setAlarmMin: Dispatch<SetStateAction<string>>
  sleepStart: string
  setSleepStart: Dispatch<SetStateAction<string>>
  sleepEnd: string
  setSleepEnd: Dispatch<SetStateAction<string>>
}

export default function AlarmSettingModal(props: Props) {
  const { open, onClose, alarmHour, setAlarmHour, alarmMin, setAlarmMin, sleepStart, setSleepStart, sleepEnd, setSleepEnd } = props

  if (!open) return null

  const handleSave = async () => {
    try {
      await orderApi.saveAlarmSettings({
        hour: Number(alarmHour),
        min: Number(alarmMin),
        sleep_start: sleepStart,
        sleep_end: sleepEnd,
      })
      // 레이아웃 글로벌 폴러가 새 주기·영업시간으로 즉시 리셋되도록 이벤트 발송
      window.dispatchEvent(new CustomEvent('alarm-settings-updated'))
      showAlert(`수집 주기: ${alarmHour}시간 ${alarmMin}분 / 영업시간: ${sleepEnd} ~ ${sleepStart} 저장완료`, 'success')
      onClose()
    } catch {
      showAlert('저장 실패', 'error')
    }
  }

  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)', backdropFilter: 'blur(4px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100 }}>
      <div style={{ background: '#1A1A1A', border: '1px solid #2D2D2D', borderRadius: '16px', padding: '2rem', width: '400px', maxWidth: '90vw' }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '1.5rem' }}>
          <h3 style={{ fontSize: '1.125rem', fontWeight: 700, color: '#E5E5E5' }}>취소 알림 설정</h3>
          <button onClick={onClose} style={{ background: 'none', border: 'none', color: '#888', fontSize: '1.25rem', cursor: 'pointer' }}>✕</button>
        </div>

        {/* 수집 주기 */}
        <div style={{ marginBottom: '1.25rem' }}>
          <label style={{ fontSize: '0.8125rem', color: '#888', display: 'block', marginBottom: '0.5rem' }}>취소주문 수집 주기</label>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <input
              type="number" min="0" max="23"
              value={alarmHour}
              onChange={e => setAlarmHour(e.target.value)}
              style={{ width: '60px', padding: '0.4rem 0.5rem', background: '#111', border: '1px solid #2D2D2D', borderRadius: '6px', color: '#E5E5E5', fontSize: '0.875rem', textAlign: 'center', outline: 'none' }}
            />
            <span style={{ color: '#888', fontSize: '0.8125rem' }}>시간</span>
            <input
              type="number" min="0" max="59"
              value={alarmMin}
              onChange={e => setAlarmMin(e.target.value)}
              style={{ width: '60px', padding: '0.4rem 0.5rem', background: '#111', border: '1px solid #2D2D2D', borderRadius: '6px', color: '#E5E5E5', fontSize: '0.875rem', textAlign: 'center', outline: 'none' }}
            />
            <span style={{ color: '#888', fontSize: '0.8125rem' }}>분</span>
          </div>
        </div>

        {/* 영업시간 */}
        <div style={{ marginBottom: '1.5rem' }}>
          <label style={{ fontSize: '0.8125rem', color: '#888', display: 'block', marginBottom: '0.5rem' }}>영업시간 (이 시간대에만 수집)</label>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <span style={{ color: '#666', fontSize: '0.8125rem' }}>시작</span>
            <input
              type="time"
              value={sleepEnd}
              onChange={e => setSleepEnd(e.target.value)}
              style={{ padding: '0.4rem 0.5rem', background: '#111', border: '1px solid #2D2D2D', borderRadius: '6px', color: '#E5E5E5', fontSize: '0.875rem', outline: 'none' }}
            />
            <span style={{ color: '#555', fontSize: '0.875rem' }}>~</span>
            <span style={{ color: '#666', fontSize: '0.8125rem' }}>종료</span>
            <input
              type="time"
              value={sleepStart}
              onChange={e => setSleepStart(e.target.value)}
              style={{ padding: '0.4rem 0.5rem', background: '#111', border: '1px solid #2D2D2D', borderRadius: '6px', color: '#E5E5E5', fontSize: '0.875rem', outline: 'none' }}
            />
          </div>
          <p style={{ fontSize: '0.72rem', color: '#555', marginTop: '0.375rem' }}>영업시간 외에는 취소주문 수집을 하지 않습니다</p>
        </div>

        <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'flex-end' }}>
          <button onClick={onClose} style={{ padding: '0.625rem 1.25rem', background: 'transparent', border: '1px solid #2D2D2D', borderRadius: '8px', color: '#888', fontSize: '0.875rem', cursor: 'pointer' }}>취소</button>
          <button
            onClick={handleSave}
            style={{ padding: '0.625rem 1.25rem', background: '#FF8C00', border: 'none', borderRadius: '8px', color: '#fff', fontSize: '0.875rem', fontWeight: 600, cursor: 'pointer' }}
          >저장</button>
        </div>
      </div>
    </div>
  )
}
