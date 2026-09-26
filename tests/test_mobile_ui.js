'use strict';
const assert=require('node:assert/strict');
require('../expman/static/mobile/model.js');
const M=globalThis.MobileModel;
const job={state:'running',command_id:0,command_ack:0,spec:{resume_supported:true}};
assert.deepEqual(M.actions(job).map(a=>a.value),['stop','cancel']);
assert.deepEqual(M.actions({...job,command_id:1}),[]);
assert.deepEqual(M.actions({...job,state:'paused'}).map(a=>a.value),['resume']);
assert.deepEqual(M.actions({...job,state:'paused',spec:{resume_supported:false}}),[]);
assert.deepEqual(M.actions({...job,state:'succeeded'}),[]);
assert.deepEqual(M.actions({...job,spec:{resume_supported:false}}).map(a=>a.value),['cancel']);
for(const [session,connected,age,busy,expected] of [
  [{permission:'control'},true,0,false,true],
  [{permission:'monitor'},true,0,false,false],
  [{permission:'control'},false,0,false,false],
  [{permission:'control'},true,15000,false,false],
  [{permission:'control'},true,0,true,false],
]) assert.equal(M.canControl(session,connected,100000-age,busy,100000),expected);
const events=[{kind:'metrics',time:1,data:{attempt:1,metrics:{step:1,loss:20}}},
  {kind:'metrics',time:2,data:{attempt:2,metrics:{step:1,loss:2}}},
  {kind:'metrics',time:3,data:{attempt:2,metrics:{step:2,loss:1}}},
  {kind:'metrics',time:4,data:{attempt:2,metrics:{step:3,loss:'not numeric'}}}];
assert.deepEqual(M.series(events,'loss',2),[{x:1,y:2},{x:2,y:1}]);
assert.equal(M.path(M.series(events,'loss',2)),'16,20 304,124');
assert.equal(M.path([{x:1,y:1},{x:1,y:1}]),'16,124 16,124');
assert.equal(M.path([]),'');
console.log('Mobile permissions, stale state, command availability and metric history passed.');
