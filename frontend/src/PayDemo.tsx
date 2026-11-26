import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { CheckCircle2, CreditCard, ShieldCheck, XCircle } from 'lucide-react'
import { api, rub } from './api'

// Демонстрационная страница оплаты: имитирует страницу ЮKassa, пока не подключён токен MCP-сервера.
// С токеном YOOKASSA_MCP_TOKEN клиент получает настоящую ссылку confirmation_url от create_payment.
export default function PayDemo() {
  const { id = '' } = useParams()
  const [p, setP] = useState<any>(null)
  const [err, setErr] = useState('')
  useEffect(() => { api(`/api/payments/demo/${id}`).then(setP).catch(e => setErr(e.message)) }, [id])
  const act = async (a: 'pay' | 'cancel') => { try { setP(await api(`/api/payments/demo/${id}/${a}`, { method: 'POST' })) } catch (e) { setErr((e as Error).message) } }
  return (
    <div className="login">
      <div className="panel paydemo">
        <div className="demo-badge">ДЕМО-РЕЖИМ · без списания денег</div>
        <h2><CreditCard size={22} /> Оплата заказа</h2>
        {err && <div className="err">{err}</div>}
        {p && (<>
          <div className="kv"><span>Документ</span><b>{p.number}</b></div>
          <div className="kv"><span>Получатель</span><span>ПАО «Софтлайн»</span></div>
          <div className="kv"><span>Сумма</span><b>{rub(p.amount)}</b></div>
          {p.status === 'pending' ? (<>
            <p className="muted small">В рабочем режиме здесь открывается платёжная страница ЮKassa (карта, СБП, SberPay): ссылку создаёт MCP-сервер ЮKassa инструментом create_payment на сумму документа.</p>
            <button className="btn" onClick={() => act('pay')}><ShieldCheck size={16} /> Оплатить (тест)</button>
            <button className="link" onClick={() => act('cancel')}>Отменить</button>
          </>) : p.status === 'succeeded'
            ? <div className="ok"><CheckCircle2 size={18} /> Оплата прошла. Вернитесь в чат — там уже есть подтверждение.</div>
            : <div className="err"><XCircle size={16} /> Платёж отменён</div>}
        </>)}
      </div>
    </div>
  )
}
