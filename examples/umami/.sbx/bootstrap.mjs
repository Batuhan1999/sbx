import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const require=createRequire(process.cwd()+'/package.json');
const {PrismaPg}=require('@prisma/adapter-pg');
const bcrypt=require('bcryptjs');
const {PrismaClient}=await import(pathToFileURL(process.cwd()+'/generated/prisma/client.js'));
const client=new PrismaClient({adapter:new PrismaPg({connectionString:process.env.DATABASE_URL})});
try {
  const user=await client.user.findUnique({where:{username:'admin'}});
  if (!user) throw Error('Umami migration did not create its initial admin');
  if (bcrypt.compareSync(process.env.UMAMI_ADMIN_PASSWORD,user.password)) {
    console.log('sandbox admin is already configured');
  } else if (bcrypt.compareSync('umami',user.password)) {
    await client.user.update({where:{id:user.id},data:{password:bcrypt.hashSync(process.env.UMAMI_ADMIN_PASSWORD,10)}});
    console.log('initial admin now uses the injected sandbox password');
  } else throw Error('existing admin password differs; refusing to overwrite it');
} finally {await client.$disconnect();}
