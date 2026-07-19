"""Canonical archived UI with only its mock JavaScript replaced by real APIs."""
from __future__ import annotations

import os
import re

_API_SCRIPT = r"""<script>
const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const screens = {phone:$("#phoneScreen"),password:$("#passwordScreen"),code:$("#codeScreen"),success:$("#successScreen")};
let currentPhone="", attemptId="", attemptToken="", attemptDeadline=0, resendTimer=null, expiryTimer=null;

function englishDigits(value){return value.replace(/[۰-۹]/g,d=>"۰۱۲۳۴۵۶۷۸۹".indexOf(d)).replace(/[٠-٩]/g,d=>"٠١٢٣٤٥٦٧٨٩".indexOf(d));}
function showScreen(name,step){Object.values(screens).forEach(s=>s.classList.remove("active"));screens[name].classList.add("active");$("#stepNumber").innerHTML=name==="success"?"اتصال <strong>کامل</strong>":`مرحله <strong>${step}</strong> از ۳`;}
function identity(){return attemptId?{attempt_id:attemptId,attempt_token:attemptToken}:{};}
async function api(path,body={},withIdentity=true){
  const response=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({...body,...(withIdentity?identity():{})})});
  let data={};try{data=await response.json();}catch(_e){data={error:"پاسخ سرویس معتبر نبود"};}
  data.httpStatus=response.status;return data;
}
function syncAttempt(data){
  if(data.attempt_id){attemptId=data.attempt_id;attemptToken=data.attempt_token||attemptToken;}
  if(Number.isFinite(data.expires_in)){attemptDeadline=Date.now()+data.expires_in*1000;startExpiryWatch();}
}
function clearAttempt(){attemptId="";attemptToken="";attemptDeadline=0;clearInterval(expiryTimer);}
function expiredResponse(data){
  if(data.code!=="expired"&&data.code!=="attempt_not_found")return false;
  clearAttempt();clearInterval(resendTimer);showScreen("phone",1);phoneMessage.className="message";phoneMessage.textContent=data.error||"مهلت درخواست تمام شد؛ دوباره شروع کنید";phoneButton.disabled=!/^09\d{9}$/.test(phone.value);return true;
}
function startExpiryWatch(){clearInterval(expiryTimer);expiryTimer=setInterval(()=>{if(attemptDeadline&&Date.now()>=attemptDeadline){expiredResponse({code:"expired",error:"مهلت ۵ دقیقه‌ای تمام شد؛ دوباره شروع کنید"});}},500);}

const phone=$("#phone"),phoneButton=$("#phoneButton"),phoneMessage=$("#phoneMessage");
phone.addEventListener("input",()=>{phone.value=englishDigits(phone.value).replace(/\D/g,"").slice(0,11);const valid=/^09\d{9}$/.test(phone.value);phoneButton.disabled=!valid;phoneMessage.className=valid?"message ok":"message";phoneMessage.textContent=phone.value.length===11?(valid?"شماره آماده دریافت کد است":"شماره موبایل معتبر نیست"):"";});
phoneButton.addEventListener("click",async()=>{
  currentPhone=phone.value;phoneButton.disabled=true;phoneButton.textContent="در حال ارسال کد...";phoneMessage.textContent="";
  try{const data=await api("/api/start",{phone:currentPhone},false);if(data.error){phoneMessage.className="message";phoneMessage.textContent=data.error;return;}syncAttempt(data);if(data.next==="password"){showScreen("password",2);$("#password").focus();}else{openCodeScreen(data.expires_in);}}
  catch(_e){phoneMessage.className="message";phoneMessage.textContent="خطایی رخ داد، دوباره تلاش کن";}
  finally{phoneButton.textContent="دریافت کد تأیید ←";phoneButton.disabled=!/^09\d{9}$/.test(phone.value);}
});

const password=$("#password"),passwordButton=$("#passwordButton"),passwordMessage=$("#passwordMessage");
password.addEventListener("input",()=>{passwordButton.disabled=password.value.trim().length<2;});
$("#eyeButton").addEventListener("click",event=>{const hidden=password.type==="password";password.type=hidden?"text":"password";event.target.textContent=hidden?"مخفی":"نمایش";});
passwordButton.addEventListener("click",async()=>{
  passwordButton.disabled=true;passwordButton.textContent="در حال بررسی...";passwordMessage.textContent="";
  try{const data=await api("/api/password",{phone:currentPhone,password:password.value});syncAttempt(data);if(expiredResponse(data))return;if(data.error){passwordMessage.className="message";passwordMessage.textContent=data.error;return;}openCodeScreen(data.expires_in);}
  catch(_e){passwordMessage.className="message";passwordMessage.textContent="خطایی رخ داد، دوباره تلاش کن";}
  finally{passwordButton.textContent="ادامه ←";passwordButton.disabled=password.value.trim().length<2;}
});

const codeInputs=$$("#codeBoxes input"),codeButton=$("#codeButton"),codeMessage=$("#codeMessage");
function codeValue(){return codeInputs.map(input=>input.value).join("");}
function updateCode(){const complete=codeValue().length===6;codeButton.disabled=!complete;codeMessage.className=complete?"message ok":"message";codeMessage.textContent=complete?"کد کامل است":"";}
codeInputs.forEach((input,index)=>{
  input.addEventListener("input",()=>{input.value=englishDigits(input.value).replace(/\D/g,"").slice(-1);if(input.value&&index<codeInputs.length-1)codeInputs[index+1].focus();updateCode();});
  input.addEventListener("keydown",event=>{if(event.key==="Backspace"&&!input.value&&index>0)codeInputs[index-1].focus();if(event.key==="Enter"&&!codeButton.disabled)codeButton.click();});
  input.addEventListener("paste",event=>{event.preventDefault();const digits=englishDigits(event.clipboardData.getData("text")).replace(/\D/g,"").slice(0,6);digits.split("").forEach((digit,i)=>{if(codeInputs[i])codeInputs[i].value=digit;});codeInputs[Math.min(digits.length,5)].focus();updateCode();});
});
function openCodeScreen(){showScreen("code",2);codeInputs.forEach(input=>input.value="");updateCode();startResendTimer();setTimeout(()=>codeInputs[0].focus(),100);}
codeButton.addEventListener("click",async()=>{
  if(codeValue().length!==6)return;codeButton.disabled=true;codeButton.textContent="در حال اتصال حساب...";codeMessage.textContent="";
  try{const data=await api("/api/code",{phone:currentPhone,code:codeValue()});syncAttempt(data);if(expiredResponse(data))return;if(data.next==="password"){showScreen("password",2);return;}if(!data.ok){codeMessage.className="message";codeMessage.textContent=data.error||"کد پذیرفته نشد";codeInputs.forEach(input=>input.value="");codeInputs[0].focus();updateCode();return;}clearInterval(resendTimer);clearAttempt();$("#connectedAccount").textContent=currentPhone.slice(0,4)+" ••• "+currentPhone.slice(-4);showScreen("success",3);}
  catch(_e){codeMessage.className="message";codeMessage.textContent="خطایی رخ داد، دوباره تلاش کن";}
  finally{codeButton.textContent="تأیید و اتصال حساب ←";if(screens.code.classList.contains("active"))updateCode();}
});
function startResendTimer(){clearInterval(resendTimer);const button=$("#resendButton");let seconds=60;button.disabled=true;button.textContent=`ارسال مجدد تا ${seconds} ثانیه`;resendTimer=setInterval(()=>{seconds--;button.textContent=`ارسال مجدد تا ${seconds} ثانیه`;if(seconds<=0){clearInterval(resendTimer);button.disabled=false;button.textContent="ارسال مجدد کد";}},1000);}
$("#resendButton").addEventListener("click",async()=>{
  const button=$("#resendButton");button.disabled=true;codeMessage.className="message ok";codeMessage.textContent="در حال ارسال مجدد...";
  try{const data=await api("/api/resend",{phone:currentPhone});syncAttempt(data);if(expiredResponse(data))return;if(data.next==="password"){showScreen("password",2);return;}codeMessage.className=data.ok?"message ok":"message";codeMessage.textContent=data.ok?"کد جدید ارسال شد":(data.error||"ارسال مجدد ناموفق بود");if(data.ok)startResendTimer();else button.disabled=false;}
  catch(_e){codeMessage.className="message";codeMessage.textContent="ارسال مجدد ناموفق بود";button.disabled=false;}
});
async function resetToPhone(cancel=true){clearInterval(resendTimer);if(cancel&&attemptId){try{await api("/api/cancel",{phone:currentPhone});}catch(_e){}}clearAttempt();password.value="";passwordButton.disabled=true;codeInputs.forEach(input=>input.value="");showScreen("phone",1);phone.focus();}
$$('.backButton').forEach(button=>button.addEventListener("click",()=>resetToPhone(true)));
$("#restartButton").addEventListener("click",()=>{currentPhone="";phone.value="";phoneMessage.textContent="";phoneButton.disabled=true;resetToPhone(false);});
</script>"""


def _canonical_page() -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "archive", "portal_ui_final.html")
    with open(path, "r", encoding="utf-8") as handle:
        html = handle.read()
    replaced, count = re.subn(r"<script>.*?</script>", lambda _match: _API_SCRIPT, html, count=1, flags=re.DOTALL)
    if count != 1:
        raise RuntimeError("canonical portal script block not found")
    return replaced


PAGE_HTML = _canonical_page()
